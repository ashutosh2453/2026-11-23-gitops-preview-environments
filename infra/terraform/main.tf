# Cluster baseline for preview environments: a system namespace and a
# least-privilege identity that previewctl deploys with. CI applies this to a
# throwaway k3d cluster and then deploys a preview *as this identity*, which
# proves the RBAC below is sufficient (and nothing more is needed).

terraform {
  required_version = ">= 1.5.0"
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.35"
    }
  }
}

provider "kubernetes" {
  config_path    = var.kubeconfig_path
  config_context = var.kube_context
}

locals {
  labels = {
    "app.kubernetes.io/managed-by" = "terraform"
    "app.kubernetes.io/part-of"    = "preview-environments"
  }
}

resource "kubernetes_namespace_v1" "system" {
  metadata {
    name   = var.system_namespace
    labels = local.labels
  }
}

resource "kubernetes_service_account_v1" "previewctl" {
  metadata {
    name      = "previewctl"
    namespace = kubernetes_namespace_v1.system.metadata[0].name
    labels    = local.labels
  }
  automount_service_account_token = false
}

resource "kubernetes_cluster_role_v1" "previewctl" {
  metadata {
    name   = "previewctl"
    labels = local.labels
  }

  # Create, label and delete preview namespaces. Kubernetes RBAC cannot scope
  # "create" by label, so the namespace-only guardrails live in previewctl's
  # policy checks and the garbage collector's managed-by filter.
  rule {
    api_groups = [""]
    resources  = ["namespaces"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  rule {
    api_groups = [""]
    resources  = ["resourcequotas", "limitranges", "services", "configmaps"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  rule {
    api_groups = ["apps"]
    resources  = ["deployments"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  # Read-only, for `kubectl rollout status`.
  rule {
    api_groups = ["apps"]
    resources  = ["replicasets"]
    verbs      = ["get", "list", "watch"]
  }

  rule {
    api_groups = [""]
    resources  = ["pods"]
    verbs      = ["get", "list", "watch"]
  }

  rule {
    api_groups = ["networking.k8s.io"]
    resources  = ["networkpolicies", "ingresses"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }

  rule {
    api_groups = ["batch"]
    resources  = ["jobs"]
    verbs      = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
}

resource "kubernetes_cluster_role_binding_v1" "previewctl" {
  metadata {
    name   = "previewctl"
    labels = local.labels
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = kubernetes_cluster_role_v1.previewctl.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = kubernetes_service_account_v1.previewctl.metadata[0].name
    namespace = kubernetes_namespace_v1.system.metadata[0].name
  }
}
