variable "kubeconfig_path" {
  description = "Path to the kubeconfig used to reach the cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubeconfig context of the cluster that hosts previews."
  type        = string
  default     = "k3d-preview"
}

variable "system_namespace" {
  description = "Namespace that holds the previewctl service account."
  type        = string
  default     = "preview-system"

  validation {
    condition     = can(regex("^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", var.system_namespace)) && length(var.system_namespace) <= 63
    error_message = "system_namespace must be a DNS-1123 label."
  }
}
