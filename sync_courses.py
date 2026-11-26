"""Refresh Moodle attachments and Echo360 transcripts for current LSE courses."""
import argparse
import fcntl
import json
import re
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.local'
MOODLE = 'https://moodle.lse.ac.uk'
COURSES = {'EC313': 15046, 'EC339': 15059, 'PH311': 16622, 'ST302': 16996}
TERM_START = date(2026, 9, 28)


def save(path, data):
    if path.exists() and path.read_bytes() == data:
        return
    with tempfile.TemporaryDirectory(dir=path.parent) as temp:
        candidate = Path(temp) / path.name
        candidate.write_bytes(data)
        candidate.replace(path)


def file_name(label):
    return re.sub(r'\s+(File|Folder)$', '', label.strip(), flags=re.I)


def selected(code, week, label):
    label = file_name(label)
    if code == 'EC339':
        return bool(re.fullmatch(rf'Lecture {week}|Problem Set {week}|Problem Set {week-1} Solutions|L{week} .+', label, re.I)
                    or (week == 9 and label == 'NEWS Currency Crisis'))
    if code == 'EC313':
        return bool(re.fullmatch(rf'Class Notes Week {week}|Problem Set {week}|Problem Set {week-1}\s*-?\s*Solutions', label, re.I)
                    or re.match(rf'^{week-3}\s|^{week-2}\s', label)
                    or (week == 9 and label == 'Course Description and Reading List'))
    if code == 'ST302':
        return bool(re.fullmatch(rf'(?:Solutions (?:to|for) )?Problem Sheet (?:{week-1}|{week})(?:\s*\(Week {week}\))?|Revision exercises!!', label, re.I))
    return False


def extract_archive(download, folder):
    files = folder / 'files'
    files.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=folder) as temp:
        path = Path(temp) / 'moodle.zip'
        download.save_as(path)
        with ZipFile(path) as archive:
            entries = [item for item in archive.infolist() if not item.is_dir()]
            names = [PurePosixPath(item.filename).name for item in entries]
            if not entries or len(set(names)) != len(names) or any(not name or name in {'.', '..'} or '\\' in name for name in names):
                raise ValueError('Moodle ZIP contains invalid or duplicate names')
            data = [(name, archive.read(item)) for name, item in zip(names, entries)]
        for name, content in data:
            save(files / name, content)
        existing = folder / 'moodle.zip'
        def signature(candidate):
            with ZipFile(candidate) as archive:
                return sorted((item.filename, item.CRC, item.file_size) for item in archive.infolist() if not item.is_dir())
        if not existing.exists() or signature(existing) != signature(path):
            path.replace(existing)
    return len(entries)


def download_files(page, code, course_id, week, folder, now):
    page.goto(f'{MOODLE}/local/downloadcenter/index.php?courseid={course_id}', wait_until='domcontentloaded')
    topics = page.locator('input[id^="id_item_topic_"]')
    topics.first.wait_for(timeout=12000)
    week_box = page.get_by_role('checkbox', name=re.compile(rf'\bAT Week {week}\b', re.I)) if code == 'PH311' else None
    topic_id = week_box.get_attribute('id', timeout=3000) if week_box is not None else None
    entries = page.locator('input[id^="id_item_resource_"], input[id^="id_item_folder_"]')
    labels = dict(entries.evaluate_all('''items => items.filter(n=>!n.disabled).map(n => [n.id,
      Array.from(n.labels ?? []).map(l => l.innerText.trim()).join(' ') || n.getAttribute('aria-label') || ''])'''))
    status = folder / '.materials.json'
    previous = json.loads(status.read_text()) if status.exists() else []
    snapshot = STATE / f'{code}-attachments.json'
    old_labels = json.loads(snapshot.read_text()) if snapshot.exists() else None
    new_ids = set()
    if old_labels is not None and week == now and code != 'PH311':
        new_ids = {key for key in labels if old_labels.get(key) != labels[key] and labels[key]}
    matches = {key for key, label in labels.items() if key in previous or key in new_ids or selected(code, week, label)}
    if not topic_id and not matches:
        if week == now:
            save(snapshot, json.dumps(labels, ensure_ascii=False).encode())
        return 0
    for box in topics.all():
        if box.get_attribute('id') != topic_id:
            box.uncheck()
    if week_box is not None:
        week_box.check()
    else:
        for key in sorted(matches):
            page.locator(f'#{key}').check()
    with page.expect_download(timeout=85000) as download:
        page.get_by_role('button', name='Create ZIP archive').click()
    count = extract_archive(download.value, folder)
    save(status, json.dumps(sorted(matches)).encode())
    if week == now:
        save(snapshot, json.dumps(labels, ensure_ascii=False).encode())
    return count


def archive_links(page, code, week, folder):
    match = re.compile(rf'\bAT Week {week}\b', re.I)
    scope = page.locator('[id^="section-"]').filter(has=page.get_by_role('heading', name=match)).first if code == 'PH311' else page.locator('#region-main')
    if not scope.count():
        return
    if code == 'PH311':
        toggle = scope.locator('[id^="toggle-"]').first
        if toggle.count() and toggle.get_attribute('aria-expanded') == 'false':
            toggle.click(timeout=3000)
    links = scope.locator('a[href*="/mod/"]').evaluate_all('a => a.map(x=>[x.textContent.trim(),x.href])')
    filtered = dict.fromkeys((file_name(name), href) for name, href in links if name and 'sesskey=' not in href and (
        code == 'PH311' or selected(code, week, name) or re.search(r'reading list|lecture recording|^Videos\b', name, re.I)))
    text = '\n'.join(f'{name}: {href}' for name, href in filtered)
    if code == 'PH311':
        text = scope.inner_text() + '\n\n' + text
    save(folder / 'moodle-week.txt', (text + '\n').encode())


def refresh_st302_slides(page, week, folder):
    parts = {8: (3,), 9: (3, 4), 10: (4,)}.get(week, ())
    if not parts:
        return
    page.goto(f'{MOODLE}/mod/folder/view.php?id=2190141', wait_until='domcontentloaded')
    for part in parts:
        filename = f'part{part}.pdf'
        link = page.get_by_role('link', name=filename, exact=True).first
        url = link.get_attribute('href', timeout=5000)
        if not url or not url.startswith(f'{MOODLE}/pluginfile.php/'):
            raise ValueError(f'ST302 下载链接无效: {filename}')
        response = page.request.get(url, timeout=15000)
        data = response.body()
        if not response.ok or not data.startswith(b'%PDF'):
            raise ValueError(f'ST302 PDF 下载失败: {filename}')
        (folder / 'files').mkdir(exist_ok=True)
        save(folder / 'files' / f'slides-{filename}', data)


def download_transcripts(page, code, week, folder, today):
    pattern = {'EC313': r'^Lecture Recordings?\b', 'EC339': rf'^Lecture {week} Recording\b',
               'PH311': r'Lecture Recordings\s*-\s*AT', 'ST302': r'^Videos\b'}[code]
    link = page.get_by_role('link', name=re.compile(pattern, re.I)).first
    if not link.count():
        return '本周录课链接尚未发布'
    with page.expect_popup(timeout=12000) as popup:
        link.click(timeout=8000)
    echo = popup.value
    try:
        echo.wait_for_url('https://echo360.org.uk/**', timeout=16000)
        start = TERM_START + timedelta(weeks=week - 1)
        days = [start + timedelta(days=i) for i in range(7) if start + timedelta(days=i) <= today]
        if not days:
            return '本周课程尚未开始'
        # EC339 links are individual classes. Other courses open the section list.
        if code != 'EC339':
            echo.wait_for_url('https://echo360.org.uk/section/**', timeout=16000)
            by_date = re.compile('|'.join(re.escape(value) for d in days for value in (d.strftime('%d%m%Y'), f'{d:%B} {d.day}, {d:%Y}')))
            rows = echo.get_by_role('button').filter(has_text=by_date)
            rows.first.wait_for(timeout=10000)
            count = rows.count()
        else:
            echo.wait_for_url('https://echo360.org.uk/lesson/**', timeout=16000)
            count = 1
        fetched = []
        for i in range(count):
            if code != 'EC339':
                rows.nth(i).click(timeout=9000)
            name = 'lecture.vtt' if count == 1 else f'lecture-{i + 1}.vtt'
            echo.locator('#more-tools-menu-toggle-btn').click(timeout=12000)
            echo.locator('#more-tools-download').click(timeout=8000)
            with echo.expect_download(timeout=20000) as download:
                echo.get_by_role('button', name='VTT Format File').click(timeout=8000)
            with tempfile.TemporaryDirectory(dir=folder) as temp:
                path = Path(temp) / name
                download.value.save_as(path)
                content = path.read_bytes()
                cues = len(re.findall(rb'(?m)^[\d:.]+ --> [\d:.]+', content))
                if not cues:
                    raise ValueError('字幕无可核对的时间码')
                save(folder / name, content)
                fetched.append(str(cues))
            if i + 1 < count:
                echo.go_back(wait_until='domcontentloaded')
        return 'VTT ' + '、'.join(fetched) + ' 段'
    finally:
        echo.close()


def run(context, today, current, weeks):
    page, downloader = context.new_page(), context.new_page()
    failures = 0
    for code, course_id in COURSES.items():
        url = f'{MOODLE}/course/view.php?id={course_id}'
        # Courses with numbered sections may skip the reading week.
        for week in weeks:
            if week not in range(1, 12) or (code == 'PH311' and week == 6) or (code == 'ST302' and week > 10):
                continue
            folder = ROOT / 'materials' / f'{code}-AT-W{week:02d}'
            if week != current and not folder.exists():
                continue
            folder.mkdir(parents=True, exist_ok=True)
            try:
                page.goto(url, wait_until='domcontentloaded')
                if '/login/' in page.url:
                    raise RuntimeError('LSE 登录已过期，请运行 lse_weekly.py --login')
                if week == current:
                    count = download_files(downloader, code, course_id, week, folder, current)
                    archive_links(page, code, week, folder)
                    print(f'{code} 第 {week} 周：Moodle {count} 个附件。', flush=True)
                    if code == 'ST302':
                        refresh_st302_slides(downloader, week, folder)
                elif (folder / 'lecture.vtt').exists():
                    continue
            except Exception as error:
                failures += 1
                print(f'{code} 第 {week} 周 Moodle：{str(error).splitlines()[0]}', file=sys.stderr, flush=True)
            try:
                status = download_transcripts(page, code, week, folder, today)
            except Exception as error:
                status = '本次未取得逐字稿，下次重试（' + str(error).splitlines()[0] + '）'
            print(f'{code} 第 {week} 周：{status}', flush=True)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--week', type=int, choices=range(1, 12))
    args = parser.parse_args()
    now = datetime.now(ZoneInfo('Europe/London'))
    today = now.date()
    current = args.week or (1 + (today - TERM_START).days // 7)
    print(f'同步 {now:%Y-%m-%d %H:%M %Z} / AT Week {current}', flush=True)
    if current not in range(1, 12):
        print('当前不在已配置的 2026 秋季教学周。', flush=True)
        return
    with (STATE / 'sync.lock').open('a') as lock, sync_playwright() as pw:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        browser = pw.chromium.launch_persistent_context(str(STATE / 'browser'), channel='chrome', headless=True,
                                                        accept_downloads=True, locale='en-GB', timezone_id='Europe/London')
        try:
            browser.add_cookies(json.loads((STATE / 'session.json').read_text()))
            weeks = [current] if args.week else [current, current - 1]
            if run(browser, today, current, weeks):
                sys.exit(1)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
