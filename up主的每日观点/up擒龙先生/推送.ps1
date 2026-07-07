# 推送观点 - up擒龙先生
# 用法：右键 → 使用PowerShell运行
$ErrorActionPreference = "Stop"
$PROJECT_DIR = "C:\Users\32299\Desktop\AAVibe coding\每日股市简报\HZT"
$UP_NAME = "up擒龙先生"

Set-Location $PROJECT_DIR

Write-Host "═══════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  推送观点: $UP_NAME" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════" -ForegroundColor Cyan

Write-Host "[1/3] 添加 $UP_NAME 的文件..." -ForegroundColor Yellow
git add "up主的每日观点/$UP_NAME/" 2>$null

Write-Host "[2/3] 提交..." -ForegroundColor Yellow
$dateStr = Get-Date -Format "yyyy-MM-dd"
git commit -m "opinions: $UP_NAME 观点更新 $dateStr" 2>$null

if ($LASTEXITCODE -ne 0) {
    Write-Host "      没有新文件需要提交" -ForegroundColor Gray
    Read-Host "按 Enter 退出"
    exit 0
}

Write-Host "[3/3] 推送到 GitHub..." -ForegroundColor Yellow
git pull --rebase origin main 2>$null
git push origin main

Write-Host "推送完成!" -ForegroundColor Green
Read-Host "按 Enter 退出"
