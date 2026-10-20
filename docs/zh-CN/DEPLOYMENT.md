# 部署指南（中文）

> CI 在 ARM64 Ubuntu 20.04 容器中验证兼容性（见 [COMPATIBILITY.md](../COMPATIBILITY.md)）。
> 每次部署仍须动态核实桌面布局，并单独记录受控验收结果。

目标环境：鲁班猫 RK3588，ARM64，Ubuntu 20.04.6，glibc 2.31，XFCE/Xvnc（实测为 `:10`，1280x720，16 位色深），
Chrome 中已打开闲鱼聊天页面。程序不使用系统自带的 Python 3.8，而是通过 Conda 安装 Python 3.10。
Conda 装在独立目录中，不会修改系统 glibc 或系统 Python。

## 1. 系统软件包

```bash
sudo apt-get update
sudo apt-get install -y scrot xdotool xclip libgl1 libglib2.0-0
# 可选的备用 OCR：
sudo apt-get install -y tesseract-ocr tesseract-ocr-chi-sim
```

## 2. 通过 Conda 安装 Python 3.10

已有 Conda 时直接复用。可用 `conda create --clone <已有py310环境> -p /opt/xianyu-monitor/env`
创建独立环境；运行时需位于 `/opt`，确保 `ProtectHome=true` 的 `xvm` 后台服务可访问。
不要使用解释器或标准库位于 `/root` 内的 venv。仅在没有适用 Conda 时执行以下安装：

```bash
curl -fsSLO https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-aarch64.sh
sudo bash Miniforge3-Linux-aarch64.sh -b -p /opt/conda
sudo mkdir -p /opt/xianyu-monitor
sudo /opt/conda/bin/conda create -y -p /opt/xianyu-monitor/env python=3.10
```

## 3. 安装程序

```bash
sudo git clone https://github.com/<owner>/xianyu-visual-monitor.git /opt/xianyu-monitor/src
cd /opt/xianyu-monitor/src
sudo /opt/xianyu-monitor/env/bin/pip install -r requirements/ocr.txt
sudo /opt/xianyu-monitor/env/bin/pip install --no-deps .
```

## 4. 用户、目录与私有配置

```bash
sudo useradd --system --home /var/lib/xianyu-monitor --shell /usr/sbin/nologin xvm
sudo install -d -o root -g xvm -m 2770 /var/lib/xianyu-monitor
sudo install -d -o root -g xvm -m 0750 /etc/xianyu-monitor
sudo install -o root -g xvm -m 0640 deploy/monitor.env.example /etc/xianyu-monitor/monitor.env
sudoedit /etc/xianyu-monitor/monitor.env
```

请填写数据库密码、`XVM_SECRET_KEY`、SMTP 账号，以及 **`XVM_NOTIFY_TO`**（私有的收件地址）。
密钥只保存在这个文件里，不会出现在代码仓库中。

## 5. MySQL

复用 1Panel 管理的现有 MySQL 8（本机 3306），不要另装 mysql-server、重建容器或清空数据卷。
Docker、Compose 也无需重装。为应用创建独立数据库和账号，随机密码应与 SSH 和 MySQL 管理员密码不同。
容器内看到的 TCP 来源通常是桥接网关，因此应用账号使用 `%`，权限只授予 `xianyu_monitor.*`；
3306 仅允许受信任主机连接。

```bash
install -m 0600 deploy/mysql-init.sql /root/xvm-init.sql
sudoedit /root/xvm-init.sql  # 只在这份私密副本中修改密码，与 monitor.env 保持一致
# 替换为 1Panel 已有容器名称；交互输入管理员密码，不在命令中传递。
docker cp /root/xvm-init.sql <已有MySQL容器名>:/tmp/xvm-init.sql
docker exec -it <已有MySQL容器名> mysql -uroot -p
# MySQL 提示符中执行 SOURCE /tmp/xvm-init.sql;
# 初始化完成后删除两份临时 SQL 文件。
sudo -u xvm env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor migrate
sudo -u xvm env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor create-admin --username owner
```

## 6. 采集进程的 X 显示权限

现有桌面属于 root。采集进程需要读取 X 授权文件并连接本地 X socket；**管理后台不需要 X 权限**，始终以 `xvm` 用户运行。

* 方案 A（默认）：采集进程以 root 运行。程序会从 `/proc` 读取 Xvnc 的 `-auth` 参数并自动发现显示号。
* 方案 B（最小权限）：在桌面会话内执行 `xhost +SI:localuser:xvm`（每次会话重启后都要重新执行），
  然后把 worker 单元改为 `User=xvm`。

存在多个 X 显示时，请设置 `XVM_CAPTURE_DISPLAY`，程序不会自行猜测。检查：

```bash
sudo env XVM_ENV_FILE=/etc/xianyu-monitor/monitor.env /opt/xianyu-monitor/env/bin/xianyu-monitor check-config
```

## 7. 区域校准

先加载示例配置，在“区域校准”页查看标注图并调整坐标，最后在页面布局正常时点击“从最新截图采集锚点”。
详见 [CONFIGURATION.md](../CONFIGURATION.md)（英文）。调整分辨率或浏览器缩放后需要重新校准；
在重新校准之前，程序会报告布局不匹配并暂停回复。

## 8. systemd

```bash
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now xianyu-monitor-web xianyu-monitor-worker
```

管理后台监听 `127.0.0.1:8080`，可用 `ssh -L 8080:127.0.0.1:8080 <开发板>` 访问。界面默认英文，右上角可切换中文。

## 9. 首次运行顺序

1. 概览页显示采集进程“正常上报”，状态为 `ok`。
2. 保持**基线模式**，积累历史客户；可在“客户”页导入其他已知客户名称。首屏可见内容并不是完整历史。
3. 处理待审核条目。
4. 配置 SMTP，先“测试连接”，再“发送测试邮件”。
5. 切换到**正式模式**，启用新客户邮件（需输入 `ENABLE`）；演练模式在您关闭之前会一直开启。
6. 关键词回复：新建的规则默认停用。请先用规则测试器验证，再用“仅审核”模式手动批准几条，
   最后才考虑启用“自动”和“回复发送”。
