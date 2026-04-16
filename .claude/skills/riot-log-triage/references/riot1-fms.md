# RIOT1 / fms-java

## Identity

- 项目别名：`fms-java`、`riot1`、`RIOT1`、`fms`
- 主要技术栈：Java、Gradle 多模块
- 日志栈：`logback`

## Start Here

- 如果问题来自“导出日志”附件，先看导出实现：
  - `service/src/main/java/com/sr/fms/service/system/controller/LogController.java`
  - 前端入口：`fms_frontend/src/views/system/system_manager.vue`
  - API 映射：`fms_frontend/src/service/api/other.js`
- 关键配置文件：`fms/src/dist/config/logback.xml`

## Practical Logging Notes

- `LogController` 有两类下载：
  - `/api/v2/system/log/file`：把 `${standard.home}/log` 整体归档成 `log.tar.gz`
  - `/api/v2/system/log/file0`：只拷贝当天和昨天的 `fms.YYYY-MM-DD.0.log`，再打成 `log.tar.gz`
- 所以 RIOT1 的附件常见是 `log.tar.gz`，而不是单个滚动日志文件。
- `LOGPATH=${standard.home}/log`
- 主业务日志滚动文件：
  - `fms.%d{yyyy-MM-dd}.%i.log`
- 监控日志滚动文件：
  - `fms-monitor.%d{yyyy-MM-dd}.%i.log`
- root logger 通过 `ASYNC` + `stdout` 输出。
- 以下 logger 会单独进入 `monitorLog`：
  - `com.sr.fms.service.common.interceptor.RequestLogInterceptor`
  - `com.baomidou.mybatisplus.plugins.PerformanceInterceptor`
- `standard.level` 会影响整体日志级别。

## Search Priorities

- 第一轮先判断拿到的是全量 `log.tar.gz` 还是“今天/昨天”的精简包。
- 先判断问题更接近主业务日志还是监控日志。
- 如果是请求入口、SQL 性能或拦截器行为，优先看 `fms-monitor.*.log`。
- 如果是常规业务异常、线程报错、服务内部流程，优先看 `fms.*.log` 和对应源码中的 logger 文本。
