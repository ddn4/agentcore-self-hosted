# Route 53 private hosted zone (runbook 1.1; decided name, runbook 3.4). `.internal` is
# reserved for private use. The frontend alias record to the NLB comes at v0.11.
resource "aws_route53_zone" "internal" {
  name    = var.private_zone_name
  comment = "Temporal frontend and UI names (agentcore-self-hosted demo)"

  vpc {
    vpc_id = module.vpc.vpc_id
  }

  # v0.11 adds the alias record; teardown (make tf-destroy) removes the zone with it.
  force_destroy = true
}
