# ============================================
# FILE: medical_billing_ai/__init__.py
# PURPOSE: Medical Billing AI Package
# ============================================

import sys
import os

# Add package root to sys.path so submodules can import directly
package_dir = os.path.dirname(os.path.abspath(__file__))
if package_dir not in sys.path:
    sys.path.insert(0, package_dir)
