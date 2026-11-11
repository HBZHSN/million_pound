import argparse
import getpass
import secrets

from .config import ROOT, Config
from .security import Security


def main():
    parser = argparse.ArgumentParser(description="家庭宠物监控预警")
    parser.add_argument("command", choices=["init", "password", "serve"])
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8097)
    args = parser.parse_args()
    data = ROOT / "data"
    if args.command == "init":
        if (data / "auth.json").exists():
            parser.error("已初始化；修改密码请使用 password 命令")
        config = Config(ROOT)
        config.save(config.current)
        password = secrets.token_urlsafe(18)
        Security.set_password(data, password)
        print("初始登录密码（仅显示一次，磁盘只保存哈希）：" + password)
    elif args.command == "password":
        password = getpass.getpass("新密码（至少 12 字符）：")
        if password != getpass.getpass("再次输入："):
            parser.error("两次密码不一致")
        try:
            Security.set_password(data, password)
        except ValueError as exc:
            parser.error(str(exc))
        print("密码已修改，重启服务后生效，旧会话及旧图片签名同时失效。")
    else:
        import uvicorn
        uvicorn.run("million_pound.app:app", host=args.host, port=args.port, workers=1,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1", access_log=False)


if __name__ == "__main__":
    main()
