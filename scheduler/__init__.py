"""Background scheduler subsystem.

Turns natural-language reminders into structured tasks (SchedulerTool),
persists them (TaskStore), watches the clock in a background thread
(SchedulerService), and delivers due tasks back to a channel (SchedulerExecutor).
"""
