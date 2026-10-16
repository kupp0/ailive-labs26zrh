# Consumed by dependent labs through lab.yaml `infra.dependency_inputs`.

# Changes only when the seed SQL changes (random_id.seed keeper). Read through
# null_resource.wait_for_seed so a consumer never sees a new value before the
# seed tables exist.
output "seed_version" {
  description = "Identifier of the current TheLook seed; changes whenever the seed is regenerated. Lab 11 uses it to re-run its reverse-ETL exports."
  value       = null_resource.wait_for_seed.triggers.seed_job
}
