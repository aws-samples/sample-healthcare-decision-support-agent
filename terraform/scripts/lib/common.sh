#!/bin/bash
# Common functions for Medical Nudging deployment scripts

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Print functions
print_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
print_error()   { echo -e "${RED}[ERROR]${NC} $1"; }
print_header()  { echo -e "\n${CYAN}============================================${NC}\n${CYAN}$1${NC}\n${CYAN}============================================${NC}"; }

# Check if command exists
command_exists() { command -v "$1" >/dev/null 2>&1; }

# Load AWS profile from terraform.tfvars
load_aws_profile() {
    local tfvars_path="${1:-terraform.tfvars}"
    local profile
    if [ -f "$tfvars_path" ]; then
        profile=$(grep 'aws_profile' "$tfvars_path" 2>/dev/null | sed 's/.*=\s*"\([^"]*\)".*/\1/' || echo "")
        if [ -n "$profile" ] && [ "$profile" != "null" ]; then
            export AWS_PROFILE="$profile"
            print_info "Using AWS profile: $AWS_PROFILE"
        fi
    fi
}

# Verify AWS credentials
verify_aws_credentials() {
    if ! aws sts get-caller-identity >/dev/null 2>&1; then
        print_error "AWS credentials not configured or invalid"
        return 1
    fi
    local account_id
    account_id=$(aws sts get-caller-identity --query Account --output text)
    print_info "AWS Account: $account_id"
    return 0
}

# Check required commands
check_required_commands() {
    local missing=()
    for cmd in "$@"; do
        if ! command_exists "$cmd"; then
            missing+=("$cmd")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        print_error "Missing required commands: ${missing[*]}"
        return 1
    fi
    return 0
}
