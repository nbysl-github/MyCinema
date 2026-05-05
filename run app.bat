@echo off

cls



where python >nul 2>&1

if 0 neq 0 (

    echo [错误] 未找到 Python，请安装 Python 3.12+

    echo 下载地址: https://www.python.org/downloads/

    pause

    exit /b 1

)



echo.

echo 正在启动服务器...

echo 服务器启动后会：

echo 1. 启动 FastAPI 服务，端口默认 5000，可在设置中修改

echo 2. 加载 config.pb 配置，自动补全缺失项

echo 3. 自动扫描开启时 - 异步扫描分类目录、监控目录变化、生成缩略图

echo    自动扫描关闭时 - 仅加载缓存数据，不执行后台任务

echo 4. 后台转码开启时 - 非 MP4 视频自动入队转码

echo    GPU 转码开启时 - 使用 NVENC 硬件加速

echo.

echo 访问地址: http://localhost:5000

echo.



python server.py

if 0 neq 0 (

    echo [错误] 服务器异常退出，错误码: 0

)



pause