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
- 原始日志时间按 `UTC+0` 记录。对照现场问题时间时，必须先换算到项目所在时区：国内项目按 `UTC+8`，其他项目按项目所在地时区。
- 如果要在结论里引用时间，优先同时写出 `UTC+0` 原始时间和换算后的现场时间，避免多方对时出错。
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
- 先判断是否属于`订单 / 车辆任务执行问题`；只要涉及车辆任务执行，默认按订单链路处理。
- `订单 / 车辆任务执行问题`首轮优先看 `bootstrap.log`，因为任务状态机和主流程门禁日志大多先在这里；`reservation.log`、`notify.log`、`mini_trace.log`、`mapf.log` 作为补充，不要先跳过去。
- `订单 / 车辆任务执行问题`优先按 `订单ID -> 车辆名称 -> 时间` 收敛；没有订单ID时，不要只靠模糊时间窗下结论。
- 再按链路 grep 对应模块中的日志文本、异常类名、订单号、车辆名称、车辆号。
- 如果涉及附件日志，先完成 `UTC+0` 到项目现场时区的换算，再筛选时间窗。

## Deadlock / Unlock Routing

对 `RIOT3 / allspark`：

- 如果问题是`死锁 / 解锁 / 死锁解除 / reroute / 路网互锁 / 交通冲突`，优先看 `mapf.log`。
- `mapf.log` 是第一优先，因为：
  - `com.standard.allspark.agvexecutor.dispatch.kernel.route.mapf` 单独打到 `mapf.log`
  - `com.standard.allspark.agvexecutor.dispatch.kernel.traffic.DeadLockDetector` 也单独打到 `mapf.log`
- 这类问题的补充文件优先级：
  - `mapf.log`
  - `bootstrap.log`
  - `reservation.log`
  - `mini_trace.log`
- 如果只是车号命中，不要先去看 `metric.log`；`metric.log` 默认视为性能监控补充，不是死锁/解锁首轮主文件。
