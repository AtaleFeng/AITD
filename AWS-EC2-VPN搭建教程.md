# AWS EC2 自建 VPN 节点完整教程

> **场景：** 在 AWS EC2（日本/新加坡/香港区域）上搭建一个自用的科学上网节点，支持 Windows、macOS、iOS、路由器多端使用。
>
> **方案：** 主推 **Xray + VLESS + REALITY**（当前抗封锁能力最强、伪装效果最好），备用 **Shadowsocks-2022**（简单可靠）。
>
> **难度：** ★★☆☆☆（跟着抄命令即可，不需要懂 Linux）
>
> **预计耗时：** 30 分钟

---

## 目录

1. [方案对比与选择](#一方案对比与选择)
2. [准备 EC2 实例](#二准备-ec2-实例)
3. [服务端搭建 - Xray REALITY 方案（推荐）](#三服务端搭建---xray-reality-方案推荐)
4. [服务端搭建 - Shadowsocks-2022 方案（备用）](#四服务端搭建---shadowsocks-2022-方案备用)
5. [客户端配置](#五客户端配置)
6. [测试与验证](#六测试与验证)
7. [性能优化与安全加固](#七性能优化与安全加固)
8. [常见问题排查](#八常见问题排查)
9. [迭代记录](#九迭代记录)

---

## 一、方案对比与选择

| 方案 | 抗封锁 | 速度 | 配置难度 | 客户端生态 | 推荐度 |
|------|--------|------|----------|------------|--------|
| **Xray + VLESS + REALITY** | ★★★★★ | ★★★★★ | ★★★ | 全平台齐全 | **首选** |
| Shadowsocks-2022 | ★★★ | ★★★★★ | ★ | 全平台齐全 | 备用 |
| OpenVPN | ★ | ★★ | ★★ | 通用 | 不推荐（特征明显） |
| WireGuard | ★ | ★★★★★ | ★★ | 通用 | 不推荐（在 GFW 下易被封） |

**为什么推荐 REALITY？**
- 不需要域名、不需要证书，开箱即用
- 流量伪装成访问真实大站（如 `www.microsoft.com`），GFW 主动探测会得到真实站点的响应，几乎无法识别
- 当前（2026 年）墙内最稳的方案之一

> 如果你完全没接触过 Linux，建议直接看「Xray 一键脚本」那一节，几条命令搞定。

---

## 二、准备 EC2 实例

### 2.1 实例选型建议

| 项目 | 建议值 | 说明 |
|------|--------|------|
| 区域（Region） | 东京 `ap-northeast-1` / 新加坡 `ap-southeast-1` / 香港 `ap-east-1` | 日本通常对国内速度最佳 |
| 实例类型 | `t3.micro` 或 `t4g.small`（ARM 更便宜） | 自用 1-2 人完全够用 |
| 操作系统 | **Ubuntu 22.04 LTS** 或 24.04 LTS | 本教程基于 Ubuntu |
| 存储 | 8-10 GB gp3 | 默认即可 |
| 弹性 IP | 强烈建议申请 1 个 | 防止重启后 IP 变化 |
| Xray 监听端口 | **优先用 40443/40444 等高位端口**，不推荐 443 | 实测 AWS 部分新实例的 443 端口对外不可达（原因未知，疑似新账号/新 IP 的隐式限制），换非常规高位端口可绕过 |

> **提示：** 如果你是 AWS 新用户，`t3.micro` 在 12 个月免费额度内基本免费；超出后约 $7-10/月。流量费才是大头（出站 $0.09/GB），日常使用控制在 100GB 以内月费可控。

### 2.2 安全组（Security Group）配置

进入 **EC2 控制台 → 安全组 → 编辑入站规则**，添加以下规则：

| 类型 | 协议 | 端口 | 来源 | 用途 |
|------|------|------|------|------|
| SSH | TCP | 22 | 你的 IP | 远程登录（不要开 0.0.0.0/0） |
| 自定义 TCP | TCP | **40443**（推荐）或 443 | 0.0.0.0/0 | Xray REALITY 监听端口 |
| 自定义 TCP | TCP | 8388 | 0.0.0.0/0 | Shadowsocks（如启用） |

> ⚠️ **重要经验：优先用非 443 端口。** 实测 AWS 部分实例（特别是新加坡区新建实例）的 443 端口对外完全不可达，原因可能是 AWS 内部对该端口的隐式限制。改用 **40443、40444、52443** 这种非常规高位端口可立即绕过。如果实在要用 443 不通，**不要瞎重装服务端**——服务端很可能是好的，直接换端口。

> 🚨 **「真实绑定的安全组」陷阱：** 一个 EC2 实例可能关联的不是你以为的那个安全组（比如同时存在 `default` 和 `launch-wizard-1`，名字易混）。**确认方法**：实例详情页 → Security 选项卡 → 看 Security groups 那一栏的 sg-id；或 SSH 进服务器跑：
> ```bash
> TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600") && curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/security-groups
> ```

> **安全提示：** 22 端口的来源一定要限制成「我的 IP」，否则会被全网爆破。

### 2.3 SSH 连接到服务器

下载 EC2 创建时给你的 `.pem` 密钥文件，假设名为 `aws-key.pem`，放在 `~/Downloads/`。

**macOS / Linux 终端：**

```bash
chmod 400 ~/Downloads/aws-key.pem
ssh -i ~/Downloads/aws-key.pem ubuntu@<你的EC2公网IP>
```

**Windows：** 推荐用 [Tabby](https://tabby.sh/) 或 Windows Terminal + OpenSSH，命令同上。也可以用 PuTTY（需先用 PuTTYgen 把 `.pem` 转成 `.ppk`）。

登录成功后，先更新系统：

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y curl wget unzip tmux
```

### 2.4 防止 SSH 频繁断连（强烈建议先做）

AWS 的 NAT 会把空闲 TCP 连接 60-350 秒回收，加上家庭宽带 NAT，挂久了必断。从**客户端 + 服务端 + 后台运行**三层面同时解决：

#### ① 服务端开启 Keepalive

```bash
sudo tee -a /etc/ssh/sshd_config > /dev/null <<EOF

# 防止空闲断连
ClientAliveInterval 60
ClientAliveCountMax 3
TCPKeepAlive yes
EOF

sudo systemctl restart ssh
```

#### ② 客户端开 Keepalive

- **Tabby：** Settings → Profiles → 编辑你的连接 → Connection 标签页 → Keepalive interval = `30`，Keepalive count max = `3`
- **macOS/Linux 命令行：** 在 `~/.ssh/config` 加：

  ```
  Host *
    ServerAliveInterval 30
    ServerAliveCountMax 3
  ```

#### ③ 用 tmux 跑长任务（最关键）

跑安装脚本前先开 tmux 会话，这样 SSH 即使掉了，脚本依然在后台跑，重连后能接着用：

```bash
tmux new -s vpn        # 新建会话
# 在 tmux 里执行 sudo -i 和后续脚本

# 主动脱离：Ctrl+B 然后按 D
# 重连后回到现场：
tmux attach -t vpn
```

---

## 三、服务端搭建 - Xray REALITY 方案（推荐）

### 3.1 一键脚本安装（小白首选）

推荐使用社区维护良好的 **mack-a/v2ray-agent** 八合一脚本，自动处理证书、伪装、防火墙等所有事项。

> ⚠️ **AWS Ubuntu 必看：** 默认登录用户是 `ubuntu`，没有 `/root/` 写入权限。**必须先切换到 root**，否则会报 `Permission denied`：
>
> ```bash
> sudo -i
> ```
>
> 看到提示符从 `ubuntu@...$` 变成 `root@...#` 之后再继续。

> 🚨 **协议选择别走错路！** mack-a 脚本菜单里有多种组合，**只选含 REALITY 的那个**（如 `VLESS + Vision + REALITY`）。**不要选** 任何带 `WS`、`gRPC`、`TLS+域名`、`Trojan` 字样的组合——那些方案会进入「初始化 Nginx 申请证书 → 请输入域名」分支，需要你额外买域名+解析+证书，跟我们「免域名免证书」的初衷背道而驰。如果不慎进入了，按 `Ctrl+C` 取消重跑即可。

```bash
wget -P /root -N --no-check-certificate "https://raw.githubusercontent.com/mack-a/v2ray-agent/master/install.sh" && chmod 700 /root/install.sh && /root/install.sh
```

按提示选择（**v3.5.14 及以上**版本的菜单）：

1. 主菜单 → 选 **`3` 一键无域名 Reality**（这是专门为免域名 REALITY 设计的快捷入口；**不要选 `1` 安装**，那个会走完整 12 步流程并要求域名+证书）
2. **端口** → 默认 `443`，或填 `40443` 等高位端口（**改了端口记得同步去 AWS 安全组放行**）
3. **回落域名** → 默认 `www.microsoft.com` 即可，也可改成 `www.tesla.com`、`www.lovelive-anime.jp`、`addons.mozilla.org` 等
4. **UUID** → 直接回车自动生成

脚本会自动：
- 安装 Xray 内核
- 生成 UUID、X25519 密钥对、shortId
- 写好配置文件
- 启动 systemd 服务
- 输出客户端导入链接（vless://...）和二维码

**记下脚本最后输出的 vless:// 分享链接和二维码**，这是你的客户端配置。

> 如果你只想用一键脚本，到这里服务端就完成了，跳到「[五、客户端配置](#五客户端配置)」。

### 3.2 手动配置（理解原理）

如果你想理解每一步在做什么，按以下步骤手动来：

#### ① 安装 Xray-core

```bash
bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
```

#### ② 生成必要参数

```bash
# 生成 UUID（用户唯一标识）
xray uuid

# 生成 X25519 密钥对（REALITY 加密用）
xray x25519

# 生成 shortId（短标识，8-16 位十六进制）
openssl rand -hex 8
```

把这三组输出记下来，下一步要用。

#### ③ 写配置文件

```bash
sudo nano /usr/local/etc/xray/config.json
```

粘贴以下内容（**记得替换 `<UUID>`、`<PRIVATE_KEY>`、`<SHORT_ID>` 三处**）：

```json
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "listen": "0.0.0.0",
      "port": 443,
      "protocol": "vless",
      "settings": {
        "clients": [
          { "id": "<UUID>", "flow": "xtls-rprx-vision" }
        ],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "www.microsoft.com:443",
          "xver": 0,
          "serverNames": ["www.microsoft.com"],
          "privateKey": "<PRIVATE_KEY>",
          "shortIds": ["<SHORT_ID>"]
        }
      },
      "sniffing": {
        "enabled": true,
        "destOverride": ["http", "tls", "quic"]
      }
    }
  ],
  "outbounds": [
    { "protocol": "freedom", "tag": "direct" },
    { "protocol": "blackhole", "tag": "block" }
  ]
}
```

按 `Ctrl+O` 保存，`Ctrl+X` 退出。

#### ④ 启动并设置开机自启

```bash
sudo systemctl restart xray
sudo systemctl enable xray
sudo systemctl status xray   # 看到 active (running) 就成功了
```

如果启动失败，用 `sudo journalctl -u xray -e` 看错误。

#### ⑤ 拼接客户端分享链接

格式：

```
vless://<UUID>@<EC2公网IP>:443?encryption=none&flow=xtls-rprx-vision&security=reality&sni=www.microsoft.com&fp=chrome&pbk=<PUBLIC_KEY>&sid=<SHORT_ID>&type=tcp&headerType=none#我的节点
```

把这条链接保存好，下一节客户端要用。

---

## 四、服务端搭建 - Shadowsocks-2022 方案（备用）

如果你只想要简单的，且用途不在墙内重点封锁地区，Shadowsocks 是最快也最简单的选择。

```bash
# 安装
sudo apt install -y shadowsocks-libev

# 生成强密码
SS_PASSWORD=$(openssl rand -base64 16)
echo "你的密码是：$SS_PASSWORD"

# 写配置
sudo tee /etc/shadowsocks-libev/config.json > /dev/null <<EOF
{
    "server": "0.0.0.0",
    "server_port": 8388,
    "password": "$SS_PASSWORD",
    "method": "2022-blake3-aes-128-gcm",
    "timeout": 300,
    "fast_open": true,
    "mode": "tcp_and_udp"
}
EOF

# 启动
sudo systemctl restart shadowsocks-libev
sudo systemctl enable shadowsocks-libev
```

客户端配置：
- 服务器：`<你的EC2公网IP>`
- 端口：`8388`
- 加密：`2022-blake3-aes-128-gcm`
- 密码：上面输出的那串

---

## 五、客户端配置

### 5.1 Windows - v2rayN

1. 下载 [v2rayN](https://github.com/2dust/v2rayN/releases)（选 `v2rayN-with-core.zip`，自带核心）
2. 解压，运行 `v2rayN.exe`
3. 主界面右键 → **从剪贴板导入批量 URL** → 粘贴上面那条 `vless://` 链接
4. 选中节点 → 双击启用
5. 系统代理 → **自动配置系统代理**（PAC 模式）

### 5.2 macOS - V2RayU 或 FoXray

**推荐 FoXray（App Store 免费）：**
1. App Store 搜索 "FoXray" 安装
2. 点 `+` → 粘贴 `vless://` 链接
3. 启用节点 → 设置 → 系统代理打开

**或 V2RayU（开源）：**
- 下载 [V2RayU](https://github.com/yanue/V2rayU/releases)
- 菜单栏 → 导入服务器 → 从剪切板导入
- 启用 + 开启 PAC 模式

### 5.3 iPhone / iPad

App Store 直接下载（需非中国区 Apple ID）：
- **Shadowrocket**（$2.99，最强，全协议支持）
- **Stash**（$3.99，体验好）
- **Streisand**（免费替代）

操作：复制 `vless://` 链接 → 打开 App → 一般会自动识别剪贴板，或点 `+` 粘贴。

### 5.4 路由器（OpenWRT）

让全屋设备自动走代理。前提是路由器刷了 OpenWRT 固件。

```bash
# SSH 进路由器
opkg update
opkg install luci-app-passwall
```

或者去 OpenWRT 软件中心装 **PassWall** / **OpenClash** 插件，导入 `vless://` 链接，设置成代理模式即可。

> 没有 OpenWRT 的话，可以买一个支持的路由器（小米 AX3000T、网件 R7800 等），刷固件后用。这一步比较折腾，建议先把电脑/手机调通再搞路由器。

---

## 六、测试与验证

### 6.1 服务端检查

```bash
# 检查 Xray 是否在跑
sudo systemctl status xray

# 检查端口是否监听
sudo ss -tlnp | grep 443

# 看实时日志
sudo journalctl -u xray -f
```

### 6.2 客户端检查

连上节点后：
1. 浏览器访问 https://www.google.com 看是否能打开
2. 访问 https://ipinfo.io 看 IP 是否变成你 EC2 的 IP
3. 访问 https://www.speedtest.net 测速

### 6.3 检查 REALITY 伪装

在不开代理的浏览器里访问 `https://<你的EC2公网IP>`，应该看到 `www.microsoft.com` 的页面（被 REALITY 回落到伪装站）——这就证明伪装生效了。

---

## 七、性能优化与安全加固

### 7.1 开启 BBR 拥塞控制（提升 30-50% 速度）

```bash
echo "net.core.default_qdisc=fq" | sudo tee -a /etc/sysctl.conf
echo "net.ipv4.tcp_congestion_control=bbr" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p

# 验证
sysctl net.ipv4.tcp_congestion_control   # 应输出 bbr
```

### 7.2 修改 SSH 端口（防爆破）

```bash
sudo nano /etc/ssh/sshd_config
# 把 #Port 22 改成 Port 22222（或其他不常用端口）
sudo systemctl restart ssh
```

**记得同步在 AWS 安全组里也改成新端口！**

### 7.3 启用防火墙

```bash
sudo ufw allow 22222/tcp     # SSH 新端口
sudo ufw allow 443/tcp        # Xray
sudo ufw enable
```

### 7.4 流量监控（防超额）

AWS 出站流量贵，建议设置预警：
- AWS 控制台 → **Billing → Budgets** → 设置月预算（如 $10）
- 或装 `vnstat`：

```bash
sudo apt install -y vnstat
vnstat              # 看流量统计
vnstat --hourly     # 按小时
```

### 7.5 自动续期/更新

```bash
# 让 Xray 每周自动更新到最新版
sudo crontab -e
# 添加一行：
0 4 * * 0 bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
```

---

## 八、常见问题排查

| 现象 | 可能原因 | 解决 |
|------|----------|------|
| 一键脚本报 `Permission denied` 写不到 `/root/install.sh` | AWS Ubuntu 默认用户是 `ubuntu`，无 root 权限 | 先 `sudo -i` 切到 root，再跑脚本 |
| 脚本进度走到「初始化 Nginx 申请证书 → 请输入域名」 | 选错协议组合，进入了 TLS+域名 分支 | `Ctrl+C` 取消，重跑脚本，选 `VLESS + Vision + REALITY`（不要选含 WS/gRPC/TLS/Trojan 的） |
| Tabby/SSH 频繁断连 | AWS NAT 回收空闲连接 | 见 2.4 节：服务端 sshd Keepalive + 客户端 Keepalive + tmux 后台运行 |
| `curl -k https://127.0.0.1` 服务端能通，但 check-host.net 显示 443 全球不可达 | AWS 实例 443 端口被某种隐式因素拦截（非安全组/NACL/防火墙） | 重装时换成非 443 端口（如 `40443`），同时在安全组放行该端口 |
| 浏览器超时但 SSH 22 正常 | 真实绑定的安全组可能不是你编辑的那个 | 用 EC2 元数据 API 查实际绑定的 SG，见 2.2 节小提示 |
| iOS 客户端连不上 | 节点信息含控制字符 / 客户端版本太老 | 删除用户重新添加（vasma → 7→5→7→4），导入时检查 UUID 是 8-4-4-4-12 格式 |
| 连不上节点 | 安全组没放行 443 | 检查 EC2 安全组入站规则 |
| 连上但打不开网页 | DNS 污染 | 客户端勾选「直连不走代理的请求用本地 DNS、走代理的用远端 DNS」 |
| 速度很慢 | 没开 BBR / 实例规格太低 / 流量打满 | 见 7.1，或换 `t3.small` / `t4g.small` |
| 突然断流 | EC2 被封 IP | AWS 控制台释放弹性 IP，重新申请一个新的（弹性 IP 释放后再申请会换新） |
| `journalctl` 报 `port already in use` | 80/443 被 nginx 等占用 | `sudo lsof -i:443` 找出来 kill 掉 |
| iOS 客户端连不上 | sni 字段缺失 | 检查 vless 链接里 `sni=www.microsoft.com` 参数 |
| 本地程序（如交易脚本、curl）填代理后报 `EOF occurred in violation of protocol (_ssl.c:1002)` | 把**服务端监听端口**（EC2 公网 IP 上的 40443）当成本地代理端口填了，本机那个端口根本没人监听，TCP 立刻 EOF | 本地程序的代理地址 = `http://127.0.0.1:<本机客户端监听端口>`。在 macOS 上用 `lsof -nP -iTCP -sTCP:LISTEN \| grep 127.0.0.1` 看本机有哪些端口在监听，常见 V2rayU/FoXray HTTP 是 `1087`，Surge/Stash 类常用 `1082`、`6152`，Clash 类常用 `7890`。**绝对不要填 EC2 公网 IP 或服务端的 40443**。另外 V2rayU 在 `11085` 监听的是控制端口不是代理（CONNECT 会返回 404），别填错 |
| 应用界面提示「代理配置已保存」但 Python 进程仍报 SSL EOF / 行为没变化 | 两个可能：①前端"保存"按钮实际没把文件写进磁盘（接口失败被吞了）；②本机有 Surge/Stash 这类 TUN 模式客户端在做透明代理，Python 的 urllib3/requests 在透明代理下 SSL 握手会偶发 EOF | 第一步 `cat <配置文件路径>` 看 `updated` 时间戳是不是真的更新了，没更新就直接手改文件。第二步**让 Python 显式走代理**而不是依赖透明拦截：在配置里写明 `proxyEnabled:true / proxyUrl:http://127.0.0.1:<本地端口>`，或在启动时用 `HTTPS_PROXY=http://127.0.0.1:1082 python3 xxx.py`。最后**一定要重启 Python 进程**，配置文件通常只在启动时加载 |

### 紧急：IP 被封了怎么办？

1. EC2 控制台 → 弹性 IP → 解除关联 → 释放
2. 重新申请弹性 IP → 关联到实例
3. 客户端配置里把 IP 改成新的
4. 长期方案：用域名 + Cloudflare CDN 套一层（进阶教程，本文不展开）

---

## 附：日常维护快查

### 查看账号 / 重新拿到 vless 链接

```bash
sudo -i                  # 切 root
tmux attach -t vpn       # 没有就用 tmux new -s vpn
vasma                    # 调出菜单
# 主菜单选 7（用户管理） → 选 1（查看账号）
```

### 重启 / 关闭 / 启动 Xray

```bash
sudo systemctl restart xray
sudo systemctl stop xray
sudo systemctl start xray
sudo systemctl status xray --no-pager
```

### 看 Xray 实时日志（连不通时排查用）

```bash
sudo journalctl -u xray -f
```

### 关键文件路径

| 文件 | 用途 |
|------|------|
| `/etc/v2ray-agent/xray/conf/` | Xray 所有配置文件目录 |
| `/etc/v2ray-agent/xray/conf/07_VLESS_vision_reality_inbounds.json` | REALITY 入站配置（端口、UUID、SNI 都在这里） |
| `/usr/local/etc/xray/config.json` | 旧版路径（手动安装时） |

### 删除/添加用户

`vasma` → 7（用户管理）→ 4（添加） / 5（删除）

### 修改回落 SNI 域名

`vasma` → 5（REALITY 管理）→ 选项里有「更换 dest / serverName」

---

## 九、迭代记录

> 按用户偏好，每次重大改进都记录在这里方便日后查阅。

| 日期 | 版本 | 变更内容 | 备注 |
|------|------|----------|------|
| 2026-04-30 | v1.0 | 初版：Xray + REALITY 主方案 + Shadowsocks-2022 备用方案 + 多端客户端 + BBR 加速 + 防火墙加固 | 初次创建 |
| 2026-04-30 | v1.1 | 修复 AWS Ubuntu 一键脚本权限问题：在 3.1 节增加 `sudo -i` 提示，常见问题表新增对应条目 | 用户实操遇到 `/root/install.sh: Permission denied` |
| 2026-04-30 | v1.2 | (1) 新增 2.4「防止 SSH 频繁断连」整节：sshd ClientAlive + Tabby Keepalive + tmux 三层方案；(2) 在 3.1 一键脚本处增加「协议选择别走错」红色警告，明确避开需要域名/证书的分支；(3) 常见问题表增补两条：脚本要求填域名、SSH 频繁断连 | 用户卡在「3/12 初始化 Nginx 申请证书 → 请输入域名」并且 Tabby 频繁掉线 |
| 2026-04-30 | v1.3 | 适配 mack-a 脚本 v3.5.14 新菜单：把 3.1 节的菜单选项改为「主菜单选 `3` 一键无域名 Reality」（之前版本是「选 1 安装 → 协议组合选 VLESS+Reality」），新菜单更直接 | 用户菜单实际是 v3.5.14，已有独立的「3.一键无域名Reality」快捷入口 |
| 2026-04-30 | v1.4 | 新增「附：日常维护快查」章节：vasma 快捷命令、查看账号路径（7→1）、重启 Xray、查日志、关键文件位置、增删用户、改 SNI；增加「方向键陷阱」警告（按方向键会导致 jq 解析错误污染配置） | 用户首次安装时按方向键导致 UUID 字段被污染，账号查看输出空白；最终通过删除脏用户+重新添加（菜单 7→5、7→4）修复 |
| 2026-04-30 | v1.5 | **重大经验沉淀**：(1) 实例选型表新增「Xray 监听端口」一行，明确推荐 40443 等高位端口，不推荐 443；(2) 安全组规则表把 443 改为 40443（推荐），新增重要警告框「优先用非 443 端口」+「真实绑定的安全组陷阱」附 EC2 元数据查询命令；(3) 常见问题表增补三条：443 全球不可达、SSH 通但 443 不通、iOS 连不上 | **真实排错过程**：服务端、安全组、ufw、iptables、NACL 全部正常，但 443 在 check-host.net 全球节点都 timeout（连海外节点都不通）；改用 40443 端口立即可达。这是 AWS 部分实例的隐式行为，原因不明。这次的踩坑链为本教程贡献了最有价值的一条避坑经验：**AWS EC2 上做 VPN 不要用 443 端口** |
| 2026-05-01 | v1.6 | **新增「服务端口 vs 本地代理端口」概念澄清**：在常见问题表新增一条「本地程序填代理地址时报 SSL EOF（_ssl.c:1002）」，说明 `EC2:40443` 是服务端监听端口，**不能直接填进本地程序**；本地程序填的应该是本机客户端（V2rayU/FoXray/Surge/Stash 等）启动后在 `127.0.0.1` 上监听的本地代理端口（HTTP 通常 1087/1082，SOCKS 通常 1086/1080），这个端口才是流量入口。同时澄清：V2rayU 主进程在 `127.0.0.1:11085` 监听的是控制/PAC 端口，不是代理端口，CONNECT 会返回 404 | **真实排错过程**：用户在 AITD 交易代理里填了 `http://127.0.0.1:40443`，启动后报 `EOF occurred in violation of protocol (_ssl.c:1002)`。诊断发现本机 40443 根本没人监听，真正在工作的是 MacPacket 进程的 1082 端口（出口为新加坡 EC2 `52.77.217.186`，正是 v1.5 里搭好的节点）。改成 `http://127.0.0.1:1082` 后 Binance / OpenAI / Anthropic 全部 200/401/405 正常返回。**核心教训：服务端监听端口（EC2 公网 IP:40443）和本地客户端代理端口（127.0.0.1:1082 之类）是两个完全不同的东西，本地程序永远只能用后者** |
| 2026-05-02 | v1.7 | **新增「界面保存 ≠ 文件落盘」+「Python 在透明代理下的 SSL 脆弱性」两条经验**：在常见问题表新增一条「应用前端显示『保存成功』但磁盘配置文件没更新 / Python 进程仍报 SSL EOF」。两个独立教训：(1) 任何依赖配置文件的应用，前端的"已保存"提示**不能盲信**，必须 `cat` 一下文件确认 `updated` 时间戳真的变了，否则进程加载的还是旧值；(2) 当本机有 Surge/Stash 这类 TUN 模式客户端在做透明代理时，**Python 的 urllib3/requests 在 SSL 握手阶段会偶发 EOF**（curl 没事是因为重试更宽松）。修复方法是让 Python 应用**显式**配置代理（`HTTPS_PROXY=http://127.0.0.1:1082` 或写进自己的 network.json），走标准 HTTP CONNECT 隧道，不要依赖透明拦截 | **真实排错过程**：用户改前端代理设置 → 截图显示「代理配置已保存并校验成功」 → 但 `cat config/network.json` 发现 `proxyEnabled:false`、`updated` 还是上个月。然而即使 AITD 认为没用代理，请求 DeepSeek 也报 SSL EOF——因为 MacPacket 在做透明代理。直接手改 network.json 把 `proxyEnabled:true / proxyUrl:http://127.0.0.1:1082` 写入 + 重启 `python3 run.py` 后，模型决策正常返回 |

### 后续可能的迭代方向

- [ ] 增加 Cloudflare CDN 中转方案（防 IP 被封）
- [ ] 增加自动切换备用节点的客户端规则（Clash/Stash）
- [ ] 增加分流规则（国内直连、流媒体走特定节点）
- [ ] 增加多用户管理（一台服务器给多人用）
- [ ] 增加 Hysteria2 / TUIC 等基于 QUIC 的新协议方案

---

## 附录：免责声明

本教程仅用于技术学习和合法的隐私保护用途，例如：访问海外学术资源、保护公网通信安全、跨境企业内网访问等。请遵守你所在地区和 AWS 服务条款的相关法律法规，因使用本教程产生的一切后果由使用者自行承担。
