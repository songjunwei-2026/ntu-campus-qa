"""生成分享二维码：扫一下直接打开问答页。

为什么要有这个脚本：机器地址会变（校园网 DHCP、换网卡、开热点），
每次变了重跑一次即可，不用手改图片。

用法：
    python make_qr.py                # 自动探测本机地址，生成 web/assets/qr_share.png
    python make_qr.py --ip 10.1.2.3  # 指定地址
    python make_qr.py --print        # 只打印地址，不生成图片

依赖：qrcode（pip install qrcode，Pillow 通常已随 Anaconda 装好）
"""
import argparse
import os
import socket
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "web", "assets", "qr_share.png")
PORT = 8001


def detect_ip():
    """取默认出口网卡的地址。不发包，只为让系统选网卡。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("223.5.5.5", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--print", dest="only_print", action="store_true")
    a = ap.parse_args()

    ip = a.ip or detect_ip()
    # /go 是稳定入口：会跳到带版本戳的地址，绕开手机里的旧缓存
    url = "http://%s:%d/go" % (ip, a.port)
    print("二维码内容：%s" % url)
    if a.only_print:
        return

    try:
        import qrcode
    except ImportError:
        print("缺少 qrcode 库，先执行：python -m pip install qrcode")
        sys.exit(1)

    img = qrcode.make(url, box_size=10, border=3)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img.save(OUT)
    print("已生成：%s（%d 字节）" % (OUT, os.path.getsize(OUT)))
    print("提醒：地址变了（换网络/重连 WiFi）需要重跑本脚本。")


if __name__ == "__main__":
    main()
