output "budget_name" {
  value = aws_budgets_budget.monthly.name
}

output "alarm_name" {
  value = aws_cloudwatch_metric_alarm.agentcore_active_sessions.alarm_name
}

output "sns_topic_name" {
  value = aws_sns_topic.guardrails.name
}
