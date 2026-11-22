"""previewctl: per-pull-request preview environments on Kubernetes."""

__version__ = "0.1.0"

MANAGED_BY = "previewctl"
LABEL_MANAGED_BY = "app.kubernetes.io/managed-by"
LABEL_PR = "previewctl/pr"
LABEL_PINNED = "previewctl/pinned"
ANN_EXPIRES = "previewctl/expires-at"
ANN_SHA = "previewctl/sha"
ANN_BRANCH = "previewctl/branch"
