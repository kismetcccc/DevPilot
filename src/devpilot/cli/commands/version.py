import devpilot


# 打印当前 devpilot 包的版本号
def cmd_version() -> None:
    print(devpilot.__version__)
