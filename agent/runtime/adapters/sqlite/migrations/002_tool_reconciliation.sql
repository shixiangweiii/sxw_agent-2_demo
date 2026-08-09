ALTER TABLE tool_executions
ADD COLUMN supports_reconcile INTEGER NOT NULL DEFAULT 0
CHECK (supports_reconcile IN (0,1));
