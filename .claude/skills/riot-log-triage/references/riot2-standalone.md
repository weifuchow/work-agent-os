# RIOT2 / riot-standalone

## Identity

- 项目别名：`riot-standalone`、`riot2`、`RIOT2`
- 主要技术栈：Java 11、Spring Boot、Maven 多模块
- 日志栈：以 `log4j2` 为主

## Start Here

- 如果问题来自“导出日志”附件，先看导出实现：
  - `bootstrap/riot-bootstrap-core/src/main/java/sr/riot/logfile/LogFileController.java`
  - `bootstrap/riot-bootstrap-core/src/main/java/sr/riot/logfile/LogFileService.java`
- 环境配置入口可先看：
  - `bootstrap/riot-bootstrap-core/src/main/resources/application-local.properties`
  - `bootstrap/riot-bootstrap-core/src/main/resources/application-dev.properties`
  - `bootstrap/riot-bootstrap-core/src/main/resources/application-test.properties`
- 这些配置会把 `logging.config` 指向 `classpath:log4j2/log4j2-local.xml` 或 `log4j2.xml`。
- 设备侧单独入口可看：
  - `device/riot-device-bootstrap/src/main/resources/log4j2/log4j2.xml`
  - `device/riot-device-bootstrap/src/main/resources/log4j2/log4j2_appender.xml`

## Practical Logging Notes

- `LogFileService.findAndCommandZipFile(...)` 会先按类型分组、按日期筛选日志，再复制到临时目录并统一打成 `.zip`。
- 这意味着用户给你的附件可能不是原始目录，而是“按时间窗聚合后再压缩”的导出包。
- `RIOT2` 日志以服务器时间为准，不要套用 `RIOT3` 的 `UTC+0` 规则；先确认服务器所在时区，再对齐问题时间窗。
- 设备侧 `log4j2.xml` 中：
  - `APP_NAME=riot-device`
  - `LOG_HOME=/data/logs/${APP_NAME}`
- RollingFile 输出规则：
  - 当前文件：`${LOG_HOME}/${APP_NAME}-${HOST_NAME}.log`
  - 滚动文件：`${LOG_HOME}/${APP_NAME}-${HOST_NAME}-%d{yyyy-MM-dd}.%i.log.gz`
- 不同模块可能有各自的 `log4j2*.xml`，不要假设全项目只有一套日志配置。

## Search Priorities

- 第一轮先判断附件是运行时原始 `.log/.log.gz`，还是 `LogFileService` 二次生成的 `.zip`。
- 先锁定是 bootstrap、device、brokerx、fcs 还是其他模块。
- 先判断是否属于`订单 / 车辆任务执行问题`；只要涉及车辆任务执行，默认按订单链路处理。
- `订单 / 车辆任务执行问题`首轮优先看 `bootstrap.log`，因为状态机、步骤推进和主流程门禁日志通常先在这里；专项文件再按问题类型补充。
- `订单 / 车辆任务执行问题`最终按 `订单ID -> 车辆名称 -> 时间` 收敛；缺订单号时，首轮先按 `车辆名称 + 时间窗 + 业务门禁词` 找 `order_candidates`。非订单问题至少保证时间和车辆名称匹配。
- 再在该模块下搜索 `log4j2*.xml`、异常类、日志文本和相关接口路径。
- 如果现场只给了一个异常类或接口报错，先从模块配置反推它会落到哪个日志目录和文件模式，并按服务器时间生成搜索窗口。

## Deadlock / Unlock Routing

对 `RIOT2 / riot-standalone`：

- 如果问题是`死锁 / 解锁 / 路权释放 / 交通冲突 / reroute`，不要硬套 `mapf.log`，因为 RIOT2 不是这套文件名。
- 这类问题的优先文件应按下面顺序看：
  - `DeadLockDetector.log`
  - `TrafficSubSystem.log`
  - `ShapeTrafficManager.log`
  - `WorldRoute.log`
  - `bootstrap.log`
- 配置依据：
  - `sr.riot.dispatch.kernel.traffic.DeadLockDetector` -> `DeadLockDetector.log`
  - `sr.riot.dispatch.kernel.traffic.TrafficSubSystem` -> `TrafficSubSystem.log`
  - `sr.riot.dispatch.kernel.traffic.ShapeTrafficManager` -> `ShapeTrafficManager.log`
  - `sr.riot.dispatch.kernel.route.WorldRoute` -> `WorldRoute.log`
- `metric.log` 也不是首轮主文件，除非你要补性能侧佐证。
