@echo off
cd /d C:\Users\Administrator\Desktop\projects\gojep
venv\Scripts\python.exe -m cli.runner run-tenders >> logs\run_tenders.log 2>&1
