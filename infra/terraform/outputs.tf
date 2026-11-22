output "service_account" {
  description = "Namespace/name of the identity previewctl deploys with."
  value       = "${kubernetes_service_account_v1.previewctl.metadata[0].namespace}/${kubernetes_service_account_v1.previewctl.metadata[0].name}"
}

output "impersonate_as" {
  description = "Value for kubectl --as when testing the role."
  value       = "system:serviceaccount:${kubernetes_service_account_v1.previewctl.metadata[0].namespace}:${kubernetes_service_account_v1.previewctl.metadata[0].name}"
}
