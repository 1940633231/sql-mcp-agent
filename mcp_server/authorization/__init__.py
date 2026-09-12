"""Authorization layer: RBAC, ACL, column visibility and row-level policies."""
from .models import AuthorizationResult, PermissionPolicy
from .manager import PolicyConflictError, get_permission_policy, get_policy_manager
from .policy import load_permission_policy
from .validation import PolicyValidationError, PolicyValidationReport, validate_policy_document
from .service import AuthorizationService

__all__ = [
    "PolicyConflictError",
    "PolicyValidationError",
    "PolicyValidationReport",
    "validate_policy_document",
    "get_permission_policy",
    "get_policy_manager",
    "AuthorizationResult",
    "AuthorizationService",
    "PermissionPolicy",
    "load_permission_policy",
]
