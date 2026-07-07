"""
盲区检验脚本 — SessionEnd hook 自动运行

检查维度（6 维）：
  1. 安全开关：ALLOW_SHORT / 风控硬上限
  2. API 权限：.env 配置完整性
  3. .gitignore：敏感文件是否排除
  4. 代码质量：全部测试通过 / 无 emoji / 无硬编码密钥
  5. 数据完整性：clean/ parquet 时间范围
  6. 待办同步：CLAUDE.md / memory 待办是否过时

输出：
  [PASS] / [FAIL] / [WARN] 三级
  FAIL > 0 → exit code 1（提醒用户处理）

用法：
  python _blind_spot_check.py              # 完整检查
  python _blind_spot_check.py --quick      # 只跑快速检查（跳过测试）
  python _blind_spot_check.py --json       # JSON 输出（供 hook 消费）
"""

import os, sys, json, re, subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta

# ── 路径 ──
PROJECT_ROOT = Path(__file__).resolve().parent
V01 = PROJECT_ROOT  # v0.1.0/
WEB3_ROOT = PROJECT_ROOT.parent  # web3量化0705/ (git root, .env/.gitignore location)

# ── 配置 ──
MAX_POSITION_HARD_LIMIT = 0.25
DAILY_LOSS_HARD_LIMIT = 0.08
MAX_CONSECUTIVE_LOSSES_LIMIT = 7

results = []  # {'check': str, 'status': 'PASS'|'FAIL'|'WARN', 'detail': str}


def check(name, passed, detail="", warn=False):
    if passed:
        results.append({'check': name, 'status': 'WARN' if warn else 'PASS', 'detail': detail})
    else:
        results.append({'check': name, 'status': 'FAIL', 'detail': detail})


# ══════════════════════════════════════
# 1. 安全开关
# ══════════════════════════════════════

def check_safety_switches():
    # ALLOW_SHORT
    try:
        from backtest.strategy_base import BaseStrategy
        allow_short = BaseStrategy.ALLOW_SHORT
        if allow_short:
            check("ALLOW_SHORT", True, "当前 = True（回测正常，实盘前改为 False）", warn=True)
        else:
            check("ALLOW_SHORT", True, "已禁用做空，做多路径验证模式")
    except Exception as e:
        check("ALLOW_SHORT 读取", False, str(e))

    # 仓位硬上限
    try:
        from position_sizer import PositionConfig
        pc = PositionConfig()
        if pc.max_position_pct <= MAX_POSITION_HARD_LIMIT:
            check(f"单笔仓位上限 ({pc.max_position_pct:.0%})", True,
                  f"≤ {MAX_POSITION_HARD_LIMIT:.0%} 硬上限")
        else:
            check(f"单笔仓位上限 ({pc.max_position_pct:.0%})", False,
                  f"超过硬上限 {MAX_POSITION_HARD_LIMIT:.0%}")
        if pc.max_single_asset_exposure <= 0.30:
            check(f"单币种敞口 ({pc.max_single_asset_exposure:.0%})", True)
        else:
            check(f"单币种敞口 ({pc.max_single_asset_exposure:.0%})", False, "超过 30%")
        if pc.max_total_exposure <= 0.80:
            check(f"总敞口 ({pc.max_total_exposure:.0%})", True)
        else:
            check(f"总敞口 ({pc.max_total_exposure:.0%})", False, "超过 80%")
    except Exception as e:
        check("仓位配置读取", False, str(e))


# ══════════════════════════════════════
# 2. .env 配置
# ══════════════════════════════════════

def check_env():
    env_path = WEB3_ROOT / '.env'
    example_path = WEB3_ROOT / '.env.example'

    if env_path.exists():
        content = env_path.read_text(encoding='utf-8')
        has_binance_key = 'BINANCE_API_KEY' in content and 'your_binance' not in content
        has_deepseek_key = 'DEEPSEEK_API_KEY' in content and 'your_key' not in content

        check(".env 存在", True)
        check("币安 API Key", True,
              "已配置" if has_binance_key else "未配置（paper 模式可用）",
              warn=not has_binance_key)
        check("DeepSeek API Key", has_deepseek_key,
              "已配置" if has_deepseek_key else "未配置",
              warn=not has_deepseek_key)

        # 检查是否有不该出现的值
        for sensitive in ['withdrawal', 'transfer', 'futures']:
            if sensitive in content.lower():
                check(f".env 含敏感词 '{sensitive}'", False, "检查 API key 权限设置")
    else:
        check(".env 存在", False, "项目根目录缺少 .env 文件")


# ══════════════════════════════════════
# 3. .gitignore 完整性
# ══════════════════════════════════════

def check_gitignore():
    gitignore_path = WEB3_ROOT / '.gitignore'
    if not gitignore_path.exists():
        check(".gitignore 存在", False, "项目根目录缺少 .gitignore")
        return

    content = gitignore_path.read_text(encoding='utf-8')
    required_entries = {
        '.env': '密钥文件',
        '*.key': '密钥文件',
        'logs/': '交易日志',
        '*.csv': '交易记录',
        '*.jsonl': '交易记录',
        '*.parquet': '数据文件',
        '__pycache__/': 'Python 缓存',
        'data/': '工作目录',
        'output/': '输出目录',
    }
    for entry, reason in required_entries.items():
        if entry in content:
            check(f"gitignore: {entry}", True, reason)
        else:
            check(f"gitignore: {entry}", False, f"{reason} 未排除")


# ══════════════════════════════════════
# 4. 代码质量
# ══════════════════════════════════════

def check_code_quality(quick=False):
    # 4a. 测试
    if not quick:
        test_files = sorted(V01.glob('test_*.py'))
        for tf in test_files:
            try:
                result = subprocess.run(
                    [sys.executable, '-X', 'utf8', str(tf)],
                    capture_output=True, text=True, timeout=60,
                    cwd=str(V01),
                    env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
                    encoding='utf-8', errors='replace',
                )
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                # exit code 0 = 全部通过
                if result.returncode == 0:
                    # 提取最后几行作为摘要
                    lines = stdout.strip().split('\n')
                    summary = ' | '.join(lines[-2:]) if len(lines) >= 2 else stdout[-80:]
                    check(f"测试: {tf.name}", True, summary[:100])
                else:
                    err_msg = (stderr or stdout)[:100]
                    check(f"测试: {tf.name}", False, err_msg)
            except subprocess.TimeoutExpired:
                check(f"测试: {tf.name}", False, "超时 (>60s)")
            except Exception as e:
                check(f"测试: {tf.name}", False, str(e)[:80])
    else:
        check("测试 (跳过)", True, "--quick 模式跳过")

    # 4b. emoji 扫描
    emoji_pattern = re.compile(
        r'[\U0001F300-\U0001F9FF'  # 杂项符号
        r'\U0001FA00-\U0001FA6F'   # 国际象棋
        r'\U0001FA70-\U0001FAFF'   # 扩展-A
        r'☀-➿'           # 杂项符号
        r'⭐✌✍☕☘☠-☣☦-☯☸-☺♈-♓♠-♨♻♿⚒-⚗⚙⚛⚜⚠⚡⚪⚫⚰⚱⚽⚾⛄-⛈⛎⛏⛑⛓⛔⛩⛪⛰-⛵⛷-⛺⛽]'
        r'[\U0001F000-\U0001F02F'   # 麻将
        r'\U0001F0A0-\U0001F0FF'   # 扑克牌
        r'\U0001F100-\U0001F64F'   # 补充符号
        r'\U0001F680-\U0001F6FF'   # 交通
        r'\U0001F780-\U0001F7FF'   # 几何形状扩展
        r'\U0001F900-\U0001F9FF]',  # 补充符号
    )
    py_files_with_emoji = []
    for py_file in V01.rglob('*.py'):
        if py_file.name.startswith('_blind_spot'):
            continue
        try:
            text = py_file.read_text(encoding='utf-8')
            if emoji_pattern.search(text):
                py_files_with_emoji.append(py_file.name)
        except Exception:
            pass
    if py_files_with_emoji:
        check("emoji 扫描", False, f"{len(py_files_with_emoji)} 个文件含 emoji: {', '.join(py_files_with_emoji[:5])}")
    else:
        check("emoji 扫描", True, "所有 .py 文件无 emoji（Windows GBK 兼容）")

    # 4c. 硬编码密钥扫描
    secret_pattern = re.compile(
        r'(api_key|secret_key|private_key|password|token)\s*=\s*["\'](?!\s*$|$|your_|YOUR_|xxx|XXXX)[a-zA-Z0-9_\-]{20,}["\']',
        re.IGNORECASE,
    )
    py_files_with_secrets = []
    for py_file in V01.rglob('*.py'):
        try:
            text = py_file.read_text(encoding='utf-8')
            if secret_pattern.search(text):
                # 排除 config_manager.py 中的模板字符串
                if 'ENV_TEMPLATE' not in text:
                    py_files_with_secrets.append(py_file.name)
        except Exception:
            pass
    if py_files_with_secrets:
        check("硬编码密钥", False, f"{len(py_files_with_secrets)} 个文件疑似含硬编码密钥")
    else:
        check("硬编码密钥", True, "未发现硬编码密钥")


# ══════════════════════════════════════
# 5. 数据完整性
# ══════════════════════════════════════

def check_data_freshness():
    clean_dir = V01 / 'clean'
    if not clean_dir.exists():
        check("clean/ 目录", False, "不存在")
        return

    parquets = list(clean_dir.glob('*.parquet'))
    if not parquets:
        check("clean/*.parquet", False, "无数据文件")
        return

    check(f"parquet 文件数", len(parquets) >= 3,
          f"{len(parquets)} 个文件 (BTC/ETH/SOL × 1h/1d)")

    # 检查最新文件的修改时间
    newest = max(parquets, key=lambda p: p.stat().st_mtime)
    newest_mtime = datetime.fromtimestamp(newest.stat().st_mtime, tz=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - newest_mtime).total_seconds() / 3600

    if age_hours < 24:
        check("数据新鲜度", True, f"最新文件 {newest.name} ({age_hours:.0f}h 前)")
    elif age_hours < 72:
        check("数据新鲜度", True, f"最新文件 {newest.name} ({age_hours:.0f}h 前)", warn=True)
    else:
        check("数据新鲜度", False, f"最新 {newest.name} ({age_hours:.0f}h 前)，建议重新拉取")


# ══════════════════════════════════════
# 6. 待办同步
# ══════════════════════════════════════

def check_todo_sync():
    claude_md = Path(os.path.expanduser('~/.claude/CLAUDE.md'))
    if claude_md.exists():
        content = claude_md.read_text(encoding='utf-8')
        if '待办追踪' in content:
            check("CLAUDE.md 待办段存在", True)
            # 检查是否含过期标记（>7 天未更新）
            date_match = re.search(r'(\d{4}-\d{2}-\d{2}) 更新', content)
            if date_match:
                update_date = datetime.strptime(date_match.group(1), '%Y-%m-%d').date()
                days_ago = (datetime.now().date() - update_date).days
                if days_ago <= 7:
                    check("待办更新日期", True, f"{update_date} ({days_ago} 天前)")
                else:
                    check("待办更新日期", False, f"{update_date} ({days_ago} 天前)，已过期")
        else:
            check("CLAUDE.md 待办段存在", False, "缺少「待办追踪」段")
    else:
        check("CLAUDE.md 存在", False, "~/.claude/CLAUDE.md 不存在")


# ══════════════════════════════════════
# 主入口
# ══════════════════════════════════════

def run_all(quick=False):
    print("=" * 60)
    print("  盲区检验")
    print("=" * 60)

    print("\n[1/6] 安全开关...")
    check_safety_switches()

    print("[2/6] .env 配置...")
    check_env()

    print("[3/6] .gitignore...")
    check_gitignore()

    print("[4/6] 代码质量...")
    check_code_quality(quick=quick)

    print("[5/6] 数据完整性...")
    check_data_freshness()

    print("[6/6] 待办同步...")
    check_todo_sync()

    # ── 汇总 ──
    passes = sum(1 for r in results if r['status'] == 'PASS')
    warns = sum(1 for r in results if r['status'] == 'WARN')
    fails = sum(1 for r in results if r['status'] == 'FAIL')

    print(f"\n{'=' * 60}")
    print(f"  结果: {passes} PASS | {warns} WARN | {fails} FAIL (共 {len(results)})")
    print(f"{'=' * 60}")

    for r in results:
        icon = {'PASS': '[PASS]', 'WARN': '[WARN]', 'FAIL': '[FAIL]'}[r['status']]
        print(f"  {icon} {r['check']}")
        if r['detail']:
            print(f"       {r['detail']}")

    if fails > 0:
        print(f"\n  [!] {fails} 项失败，请处理后再继续开发。")
        return 1
    elif warns > 0:
        print(f"\n  [~] {warns} 项警告，建议关注。")
    else:
        print(f"\n  [OK] 全部通过。")

    return 0


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--quick', action='store_true', help='跳过测试运行')
    parser.add_argument('--json', action='store_true', help='JSON 输出')
    args = parser.parse_args()

    exit_code = run_all(quick=args.quick)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    sys.exit(exit_code)
