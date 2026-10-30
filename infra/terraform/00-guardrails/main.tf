# Guardrails before any spend (plan v0.8 step 0). Applied at the first AWS touch and
# destroyed last (v1.0 `make down-all`).

# Monthly cost budget: 80 % forecasted and 100 % actual, by email.
resource "aws_budgets_budget" "monthly" {
  name         = "agentcore-demo-monthly"
  budget_type  = "COST"
  limit_amount = format("%.2f", var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
}

# Alarm target. No subscription is created here: the user approved the email address
# for the Budgets notification only. Subscribe by hand if wanted (story v0.8).
resource "aws_sns_topic" "guardrails" {
  name = "agentcore-demo-guardrails"
}

# AgentCore sessions (AWS Q8, answered at v0.8): AgentCore publishes ActiveSessionCount,
# a real-time gauge, to AWS/Bedrock-AgentCore with dimension Service=AgentCore.Runtime.
# It is account-wide (no per-runtime dimension), so the threshold sits above the other
# runtimes' baseline. Per-runtime counts: scripts/agentcore-sessions.sh (metric Sessions).
resource "aws_cloudwatch_metric_alarm" "agentcore_active_sessions" {
  alarm_name          = "agentcore-demo-active-sessions"
  alarm_description   = "AgentCore Runtime sessions left running (agentcore-self-hosted demo guardrail)"
  namespace           = "AWS/Bedrock-AgentCore"
  metric_name         = "ActiveSessionCount"
  dimensions          = { Service = "AgentCore.Runtime" }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  comparison_operator = "GreaterThanThreshold"
  threshold           = var.active_sessions_threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.guardrails.arn]
}
