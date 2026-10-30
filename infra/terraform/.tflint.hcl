# tflint for every stack under infra/terraform (scripts/tf.sh lint, CI).
# Only the bundled terraform ruleset: the AWS plugin needs a GitHub download per run
# (`tflint --init`), which CI and `make check` avoid. Recorded in docs/versions.md.
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
