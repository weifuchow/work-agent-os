# RIOT3 / allspark

## Identity

- 项目别名：`allspark`、`riot3`、`RIOT3`
- 主要技术栈：Java 11、Spring Boot、Gradle
- 日志栈：`logback`

## Start Here

- 如果问题来自“导出日志”附件，先看导出实现：
  - `packages/presentation/src/main/java/com/standard/allspark/v4/presentation/misc/controller/SystemLogFilesController.java`
  - `packages/presentation/src/main/java/com/standard/allspark/presentation/system/controller/LogFileController.java`
  - `packages/presentation/src/main/java/com/standard/allspark/presentation/system/presenter/LogFilePresenter.java`
  - `packages/presentation/src/main/java/com/standard/allspark/presentation/system/utils/TarGzMultiFile.java`
- 主入口配置：`apps/bootstrap/src/main/resources/logback/logback-file.xml`
- 重点 include：
  - `logback/base.xml`
  - `logback/minitrace.xml`
  - `logback/mapf.xml`
  - `logback/metric.xml`
  - `logback/notify.xml`
  - `logback/mrs.xml`
  - `logback/reservation.xml`

## Practical Logging Notes

- `LogFilePresenter` 同时存在两类导出产物：
  - 直接 `ZipUtil.zip(...)` 生成的 `.zip`
  - `zipBy(...)` / `TarGzMultiFile.createTarGz(...)` 生成的 `tar.gz`
- 导出逻辑会先按时间和文件路径挑文件，再做二次压缩，所以拿到附件后先认清它是“原始滚动日志”还是“导出整包”。
- 默认 root appender 指向 `FILE`。
- 典型专属日志文件：
  - `bootstrap.log`
  - `mapf.log`
  - `metric.log`
  - `notify.log`
  - `mrs.log`
  - `reservation.log`
  - `mini_trace.log`
- 专属 logger 常见 `additivity=false`，意味着日志可能只进专属文件，不一定回流到主日志。
- 本项目已有本地 skill：`.claude/skills/log-analysis/SKILL.md`。
  - 用它做日志文件路由定位、滚动日志搜索和批量日志筛选。
  - 你负责把它的结果和代码、配置、现场现象合并成最终结论。

## Search Priorities

- 第一轮先确认附件来自哪个导出入口，以及最终是 `.zip` 还是 `tar.gz`。
- 先确认问题属于哪条链路：调度、预约、MRS、通知、指标、设备。
- 再按链路 grep 对应模块中的日志文本、异常类名、订单号、车辆号。
- 如果涉及附件日志，优先把事故时间换算成日志使用的时区后再筛选。
