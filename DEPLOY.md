# Ubuntu 22.04 systemd 部署步骤

下面的命令在监控服务器上执行。训练发生在另一台机器上，训练机器只需能够通过 HTTPS 访问该域名。

## 1. 部署前检查

1. 为监控域名添加 `A` 记录，指向服务器公网 IPv4。例如：`monitor.example.com -> 1.2.3.4`。
2. 云厂商安全组和 Ubuntu 防火墙放行 TCP `22`、`80`、`443`。
3. 不要放行 `8765`；应用只监听 `127.0.0.1:8765`。
4. 在 QQ 邮箱设置中开启 SMTP，并生成“授权码”。授权码不是 QQ 登录密码。

## 2. 上传并安装

将整个 `yolo-experiment-monitor` 目录上传到服务器，然后执行：

```bash
cd yolo-experiment-monitor/server
sudo bash install.sh
```

安装脚本会：

- 安装 Python 3、Nginx 和 Certbot；
- 创建无登录权限的 `experiment-monitor` 系统用户；
- 安装 systemd 服务；
- 为 API token 和会话签名生成随机密钥；
- 保留已有配置，不会在重复执行时覆盖密钥或数据库。

## 3. 配置服务和 QQ 邮件

```bash
sudo nano /etc/experiment-monitor.env
```

至少修改：

```dotenv
MONITOR_PUBLIC_URL=https://你的监控域名
MONITOR_ADMIN_PASSWORD=你的网页管理密码
QQ_SMTP_USER=你的QQ邮箱@qq.com
QQ_SMTP_AUTH_CODE=QQ邮箱生成的SMTP授权码
MONITOR_MAIL_TO=接收通知的邮箱
```

记录 `MONITOR_API_TOKEN`，后面需要把同一个 token 配置到训练机器。不要把该文件或 token 提交到 Git。

默认每日 03:15（带最多 10 分钟随机延迟）创建压缩 SQLite 备份，保存在
`/opt/experiment-monitor/data/backups`。默认最多保留 14 份/14 天，且
`MONITOR_RUN_RETENTION_DAYS=0`，不会自动删除实验记录。

启动并检查：

```bash
sudo systemctl restart experiment-monitor
sudo systemctl status experiment-monitor --no-pager
curl http://127.0.0.1:8765/health
systemctl status experiment-monitor-backup.timer --no-pager
sudo systemctl start experiment-monitor-backup.service
sudo ls -lh /opt/experiment-monitor/data/backups
```

健康检查应返回类似：`{"ok": true, ...}`。

## 4. 配置 Nginx 和 HTTPS

将模板中的域名替换为真实域名：

```bash
cd yolo-experiment-monitor/server
sed 's/MONITOR_DOMAIN/你的监控域名/g' nginx-site.conf.template | \
  sudo tee /etc/nginx/sites-available/experiment-monitor >/dev/null
sudo ln -sfn /etc/nginx/sites-available/experiment-monitor /etc/nginx/sites-enabled/experiment-monitor
sudo nginx -t
sudo systemctl reload nginx
```

签发并自动配置 Let's Encrypt HTTPS 证书：

```bash
sudo certbot --nginx -d 你的监控域名
```

Certbot 会询问证书到期通知邮箱。完成后可直接访问 `https://你的监控域名`，浏览实验列表、详情、对比和实时数据无需登录。删除、收藏、分组和标签修改会在操作时要求输入 `MONITOR_ADMIN_PASSWORD`。

## 5. 接入训练机器

复制 `client/yolo_monitor.py` 到你的训练项目。设置环境变量：

Linux/macOS：

```bash
export YOLO_MONITOR_URL='https://你的监控域名'
export YOLO_MONITOR_TOKEN='服务器上的MONITOR_API_TOKEN'
```

Windows PowerShell：

```powershell
$env:YOLO_MONITOR_URL='https://你的监控域名'
$env:YOLO_MONITOR_TOKEN='服务器上的MONITOR_API_TOKEN'
```

将常规训练：

```python
results = model.train(data="data.yaml", epochs=100)
```

改为：

```python
from yolo_monitor import YoloExperimentMonitor

monitor = YoloExperimentMonitor(experiment_name="实验名称")
results = monitor.train(model, data="data.yaml", epochs=100)
```

此包装器会添加 Ultralytics 官方回调，并在异常时上报失败后重新抛出原异常。监控网络故障不会中断训练。

## 6. 运维命令

```bash
sudo systemctl status experiment-monitor
sudo systemctl restart experiment-monitor
sudo journalctl -u experiment-monitor -f
sudo nginx -t
sudo certbot renew --dry-run
```

数据库位于 `/opt/experiment-monitor/data/monitor.db`。配置位于 `/etc/experiment-monitor.env`，权限应为 `600`。

## 7. 更新

重新上传新版本后，在 `server` 目录再次运行：

```bash
sudo bash install.sh
sudo systemctl restart experiment-monitor
```

安装脚本会保留配置和 SQLite 数据。
