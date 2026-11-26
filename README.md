# LSE Course Sync

在 Mac 上自动下载本周 Moodle 附件和 Echo360 带时间码的逐字稿。默认每周二、三、四 20:00 运行，资料按课程和周次保存在 `materials/`。当前配置覆盖 2026/27 秋季学期的 EC313、EC339、PH311 和 ST302。

## 首次运行

需要 macOS、Google Chrome 和 Python 3.11 或更新版本。在仓库目录执行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python lse_weekly.py --login --week 9
.venv/bin/python sync_courses.py
```

在专用 Chrome 窗口完成 LSE 登录。也可以给 `sync_courses.py` 传 `--week 9` 补拉指定周次。

## 定时运行

```sh
.venv/bin/python install_schedule.py
```

定时任务使用 Mac 的本地时间。保持 Mac 开机、在线并登录，日志见 `.local/sync.log`。LSE 会话过期时，再运行一次登录命令。程序会重试尚未发布的逐字稿，并保留只提供链接的资料入口。

下载的资料、日志和浏览器会话留在本机，并已加入 `.gitignore`。
