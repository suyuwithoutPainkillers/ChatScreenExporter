@echo off
chcp 65001 >nul
python chat_screen_exporter.py run --config config.json
pause
