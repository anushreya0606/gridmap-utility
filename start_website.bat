@echo off
rem Local use: only this computer can open the site.
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt
python app.py
pause
