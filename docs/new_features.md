# New Features Guide

本文档介绍 digitalplat-auto-register 新增的四大功能模块。

## 功能一：账户池管理 (Account Pool)

账户池管理模块提供多账户存储、智能选择、健康监控和生命周期管理功能。

### 核心特性

- **SQLite 持久化存储** - 所有账户数据自动保存到本地数据库
- **多种选择策略** - 支持随机、轮询、LRU、最高成功率、加权随机
- **标签过滤** - 通过标签对账户进行分类和筛选
- **健康监控** - 自动跟踪账户成功率、自动标记异常账户
- **事件历史** - 记录账户的所有操作事件

### 使用示例

```python
from digitalplat_auto_register.core.account_pool import AccountPool, SelectionStrategy
from digitalplat_auto_register.types import UserProfile

# 创建账户池实例
pool = AccountPool(db_path="account_pool.db")

# 添加账户
profile = UserProfile(
    username="myuser",
    email="myemail@example.com",
    fullname="My Name",
    phone="+1-555-123-4567",
    password="SecurePass123!"
)
entry = pool.add_account(profile, tags=["premium", "verified"])

# 选择可用账户
account = pool.select_account(
    strategy=SelectionStrategy.LEAST_RECENTLY_USED,
    tags=["premium"]
)

# 记录使用结果
pool.record_usage(account.id, success=True, domain="mydomain.com")

# 获取健康报告
health = pool.health_check()

# 导出账户
pool.export_accounts("accounts.json", format="json")
```

### CLI 命令

```bash
# 查看池状态
python -m digitalplat_auto_register.cli_enhanced pool

# 添加账户
python -m digitalplat_auto_register.cli_enhanced pool-add --username user1 --email u@test.com

# 列出所有账户
python -m digitalplat_auto_register.cli_enhanced pool-list

# 健康检查
python -m digitalplat_auto_register.cli_enhanced pool-health
```

---

## 功能二：统计面板 (Statistics Dashboard)

统计模块提供详细的运营数据收集、分析和可视化展示。

### 核心特性

- **事件驱动指标** - 轻松记录各种操作指标
- **时间序列聚合** - 按小时、天、周自动聚合数据
- **成功率追踪** - 自动计算各类操作的成功率
- **实时日志聚合** - 内存中收集最近日志用于快速查询
- **数据导出** - 支持 JSON/CSV 格式导出历史数据

### 使用示例

```python
from digitalplat_auto_register.core.statistics import StatisticsCollector, MetricType

# 创建统计收集器
stats = StatisticsCollector(db_path="statistics.db")

# 记录注册结果
stats.record_registration(
    username="myuser",
    email="myemail@example.com",
    success=True,
    duration=5.2,
    domain="mydomain.com"
)

# 记录通用指标
stats.record_metric(MetricType.TURNSTILE_SOLVED, labels={"method": "remote"})

# 获取统计摘要
summary = stats.get_summary(days=7)
print(f"成功率: {summary.registration_success_rate:.1%}")
print(f"总注册: {summary.total_registrations}")

# 获取时间序列数据
hourly = stats.get_time_series(MetricType.REGISTRATION_SUCCESS, hours=24)
```

### CLI 命令

```bash
# 查看统计面板
python -m digitalplat_auto_register.cli_enhanced stats

# 查看最近7天统计
python -m digitalplat_auto_register.cli_enhanced stats --days 7
```

---

## 功能三：增强日志 (Enhanced Logging)

增强日志模块提供结构化日志、上下文追踪、性能监控等功能。

### 核心特性

- **结构化 JSON 日志** - 便于日志收集和分析
- **上下文感知** - 自动附加操作上下文（账户ID、域名等）
- **日志聚合器** - 内存中保留最近日志供实时查询
- **性能追踪** - 自动记录函数执行时间和性能指标
- **彩色输出** - 终端中彩色显示不同级别日志

### 使用示例

```python
from digitalplat_auto_register.utils.enhanced_logging import (
    setup_enhanced_logging,
    get_logger,
    LogContext,
    log_timing,
    track_performance,
    log_aggregator
)

# 初始化增强日志
setup_enhanced_logging(
    level="INFO",
    log_file="app.log",
    structured=True  # 输出 JSON 格式到文件
)

# 创建上下文日志
context = LogContext(operation="registration", account_id="acc_123")
log = get_logger("my_module", context)
log.info("Starting registration", domain="example.com")

# 性能追踪装饰器
@track_performance("registration")
async def do_registration():
    # 执行注册...
    pass

# 计时上下文管理器
with log_timing("batch_registration", log):
    # 批量注册操作...
    pass

# 查询最近日志
recent = log_aggregator.get_recent_events(limit=10)
errors = log_aggregator.get_error_summary()
```

### CLI 命令

```bash
# 查看最近日志
python -m digitalplat_auto_register.cli_enhanced logs

# 只看错误日志
python -m digitalplat_auto_register.cli_enhanced logs --level ERROR

# 查看最近30分钟的日志
python -m digitalplat_auto_register.cli_enhanced logs --minutes 30
```

---

## 功能四：增强 CLI (Enhanced CLI)

增强 CLI 提供进度条、交互式向导、美观的表格输出等功能。

### 核心特性

- **进度条显示** - 批量操作时显示实时进度
- **交互式配置向导** - 引导用户创建配置文件
- **彩色表格输出** - 美观的数据展示
- **交互式 Shell** - 进入交互式命令模式
- **ASCII 艺术横幅** - 启动时显示品牌标识

### CLI 命令

```bash
# 显示横幅
python -m digitalplat_auto_register.cli_enhanced banner

# 运行配置向导
python -m digitalplat_auto_register.cli_enhanced wizard --config config.yaml

# 进入交互模式
python -m digitalplat_auto_register.cli_enhanced interactive

# 批量注册（带进度条）
python -m digitalplat_auto_register.cli_enhanced batch-register --count 10 --delay 5
```

### 交互式模式示例

进入交互模式后，支持以下命令：

- `status` - 显示池和统计摘要
- `list` - 列出可用账户
- `health` - 检查账户健康状态
- `logs` - 显示最近日志
- `clean` - 清理旧数据
- `help` - 显示帮助
- `quit` - 退出交互模式

---

## 依赖项

新功能需要以下依赖（已在 requirements.txt 中）：

- `rich>=13.0.0` - 用于表格、进度条、彩色输出
- `loguru>=0.7.0` - 日志框架（已有）
- `click>=8.1.0` - CLI 框架（已有）
- `pydantic>=2.5.0` - 数据验证（已有）
- `sqlite3` - Python 标准库，用于数据持久化

## 数据库文件

新功能会创建以下数据库文件：

- `account_pool.db` - 账户池数据
- `statistics.db` - 统计数据
