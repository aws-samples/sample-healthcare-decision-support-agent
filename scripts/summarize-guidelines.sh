#!/bin/bash
# Summarize medical guidelines using claude CLI or kiro-cli
#
# Usage:
#   ./scripts/summarize-guidelines.sh [pdf_path] [output_dir] [--force]
#
# Examples:
#   # Local PDFs - batch process all guidelines
#   ./scripts/summarize-guidelines.sh guidelines/pdfs/
#
#   # Local PDF - single guideline
#   ./scripts/summarize-guidelines.sh guidelines/pdfs/ADA/ada-standards-2026.pdf
#
#   # S3 PDFs - batch process from S3 bucket
#   ./scripts/summarize-guidelines.sh s3://your-bucket/guidelines/
#
#   # Use kiro-cli instead of claude
#   AGENT=kiro ./scripts/summarize-guidelines.sh guidelines/pdfs/
#
#   # Use a different model
#   MODEL=sonnet ./scripts/summarize-guidelines.sh guidelines/pdfs/
#
#   # Force reprocessing even if summary exists
#   ./scripts/summarize-guidelines.sh guidelines/pdfs/ --force
#
# Environment Variables:
#   AGENT        - CLI agent to use: "claude" (default) or "kiro"
#   MODEL        - Model to use: "opus" (default), "sonnet", "haiku", or full model ID
#   AWS_PROFILE  - AWS profile for S3 access (optional)
#   FORCE        - Set to "1" to force reprocessing (alternative to --force flag)

set -euo pipefail

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Parse arguments
PDF_PATH=""
OUTPUT_DIR=""
FORCE="${FORCE:-0}"

for arg in "$@"; do
    case "$arg" in
        --force|-f)
            FORCE=1
            ;;
        *)
            if [ -z "$PDF_PATH" ]; then
                PDF_PATH="$arg"
            elif [ -z "$OUTPUT_DIR" ]; then
                OUTPUT_DIR="$arg"
            fi
            ;;
    esac
done

# Defaults
PDF_PATH="${PDF_PATH:-guidelines/pdfs}"
OUTPUT_DIR="${OUTPUT_DIR:-guidelines}"
AWS_PROFILE="${AWS_PROFILE:-}"
AGENT="${AGENT:-claude}"
MODEL="${MODEL:-opus}"

# Store the base PDF directory for computing relative paths
PDF_BASE_DIR=""

# Allowed tools for scoped permissions (security-conscious alternative to --dangerously-skip-permissions)
# These are the minimal tools needed by the guidelines-summarizer SOP
ALLOWED_TOOLS=(
    "Read"
    "Write"
    "Edit"
    "Glob"
    "Bash(python3:*)"
    "Bash(cd:*)"
    "Bash(uv:*)"
    "Bash(source:*)"
    "Bash(git log:*)"
    "Bash(mkdir:*)"
    "Bash(cat:*)"
    "Bash(ls:*)"
)

# SOP file
SOP_FILE="$PROJECT_DIR/sops/guidelines-summarizer.sop.md"

# Validate SOP exists
if [ ! -f "$SOP_FILE" ]; then
    echo "Error: SOP file not found at $SOP_FILE"
    exit 1
fi

# Load SOP content
SOP_CONTENT=$(cat "$SOP_FILE")

# Create output directories
mkdir -p "$OUTPUT_DIR/summaries"

# Handle catalog.json based on force flag
CATALOG_FILE="$OUTPUT_DIR/catalog.json"
if [ "$FORCE" = "1" ] && [ -f "$CATALOG_FILE" ]; then
    # Backup existing catalog before overwriting
    BACKUP_FILE="$OUTPUT_DIR/catalog.json.bak.$(date +%Y%m%d_%H%M%S)"
    echo "Force mode: backing up catalog to $BACKUP_FILE"
    cp "$CATALOG_FILE" "$BACKUP_FILE"
    # Remove catalog so it gets regenerated fresh
    rm "$CATALOG_FILE"
fi

# Function to compute summary path from PDF path
# Mirrors the directory structure: pdfs/ADA/doc.pdf -> summaries/ADA/doc.md
get_summary_path() {
    local pdf="$1"
    local pdf_filename
    local summary_filename
    local relative_path
    local relative_dir

    pdf_filename=$(basename "$pdf")
    summary_filename="${pdf_filename%.pdf}.md"

    if [ -n "$PDF_BASE_DIR" ]; then
        # Get absolute path of the PDF
        local abs_pdf
        abs_pdf=$(cd "$(dirname "$pdf")" && pwd)/$(basename "$pdf")
        # Compute relative path from PDF_BASE_DIR
        relative_path="${abs_pdf#$PDF_BASE_DIR/}"
        relative_dir=$(dirname "$relative_path")
        if [ "$relative_dir" = "." ]; then
            echo "$OUTPUT_DIR/summaries/$summary_filename"
        else
            echo "$OUTPUT_DIR/summaries/$relative_dir/$summary_filename"
        fi
    else
        # Single file - just use the filename
        echo "$OUTPUT_DIR/summaries/$summary_filename"
    fi
}

# Function to process a single PDF
process_pdf() {
    local pdf="$1"
    local mode="${2:-single}"
    local summary_path
    summary_path=$(get_summary_path "$pdf")
    local summary_dir
    summary_dir=$(dirname "$summary_path")

    # Check if summary already exists
    if [ -f "$summary_path" ] && [ "$FORCE" != "1" ]; then
        echo "Skipping: $pdf (summary exists at $summary_path)"
        echo "  Use --force to reprocess"
        return 0
    fi

    # Create summary directory if needed
    mkdir -p "$summary_dir"

    echo "========================================"
    echo "Processing: $pdf"
    echo "Summary:    $summary_path"
    echo "Mode: $mode"
    echo "Agent: $AGENT"
    echo "Model: $MODEL"
    echo "========================================"

    # Build prompt with SOP and PDF path
    local PROMPT="Follow this SOP to analyze and summarize the guideline:

$SOP_CONTENT

---

Parameters:
- pdf_path: $pdf
- output_dir: $OUTPUT_DIR
- summary_path: $summary_path
- mode: $mode

Please analyze the PDF and generate the outputs as specified in the SOP.
IMPORTANT: Write the markdown summary to: $summary_path"

    if [ "$AGENT" = "claude" ]; then
        # Claude CLI in print mode with scoped tool permissions
        # Build --allowedTools arguments
        local TOOL_ARGS=""
        for tool in "${ALLOWED_TOOLS[@]}"; do
            TOOL_ARGS="$TOOL_ARGS --allowedTools \"$tool\""
        done

        echo "$PROMPT" | eval "claude --print --model \"$MODEL\" $TOOL_ARGS"
    elif [ "$AGENT" = "kiro" ]; then
        # kiro-cli with trusted tools
        # Build --trust-tools arguments
        local TOOL_ARGS=""
        for tool in "${ALLOWED_TOOLS[@]}"; do
            TOOL_ARGS="$TOOL_ARGS --trust-tools \"$tool\""
        done

        echo "$PROMPT" | eval "kiro --non-interactive --model \"$MODEL\" $TOOL_ARGS"
    else
        echo "Error: Unknown agent '$AGENT'. Use 'claude' or 'kiro'."
        exit 1
    fi

    echo ""
    echo "Completed: $pdf"
    echo ""
}

# Handle S3 or local path
TEMP_DIR=""
ORIGINAL_PDF_PATH="$PDF_PATH"

if [[ "$PDF_PATH" == s3://* ]]; then
    # Download from S3 to temp dir
    TEMP_DIR=$(mktemp -d)

    echo "Downloading PDFs from S3..."
    echo "Source: $PDF_PATH"
    echo "Temp dir: $TEMP_DIR"

    AWS_PROFILE="$AWS_PROFILE" aws s3 sync "$PDF_PATH" "$TEMP_DIR" \
        --exclude "*" \
        --include "*.pdf" \
        --include "**/*.pdf"

    PDF_PATH="$TEMP_DIR"
    echo "Downloaded to: $PDF_PATH"
    echo ""
fi

# Cleanup function
cleanup() {
    if [ -n "$TEMP_DIR" ] && [ -d "$TEMP_DIR" ]; then
        echo "Cleaning up temp directory..."
        rm -rf "$TEMP_DIR"
    fi
}
trap cleanup EXIT

# Process PDFs
PROCESSED=0

if [ -f "$PDF_PATH" ]; then
    # Single file - no base directory
    PDF_BASE_DIR=""
    process_pdf "$PDF_PATH" "single"
    PROCESSED=1
elif [ -d "$PDF_PATH" ]; then
    # Directory - set base for relative path computation
    PDF_BASE_DIR=$(cd "$PDF_PATH" && pwd)

    # Count PDFs
    PDF_COUNT=$(find "$PDF_PATH" -name "*.pdf" -type f | wc -l)
    echo "Found $PDF_COUNT PDF(s) to process"
    if [ "$FORCE" = "1" ]; then
        echo "Force mode: will reprocess existing summaries"
    fi
    echo ""

    if [ "$PDF_COUNT" -eq 0 ]; then
        echo "No PDF files found in: $PDF_PATH"
        exit 1
    fi

    # Determine mode based on count
    MODE="batch"
    if [ "$PDF_COUNT" -eq 1 ]; then
        MODE="single"
    fi

    # Process each PDF (sorted by file size, smallest first for faster initial progress)
    find "$PDF_PATH" -name "*.pdf" -type f -exec ls -la {} \; | awk '{print $5, $9}' | sort -n | awk '{print $2}' | while read -r pdf; do
        process_pdf "$pdf" "$MODE"
    done
else
    echo "Error: Path not found: $PDF_PATH"
    exit 1
fi

echo "========================================"
echo "Summary"
echo "========================================"
echo "Source: $ORIGINAL_PDF_PATH"
echo "Output: $OUTPUT_DIR"
echo "Catalog: $OUTPUT_DIR/catalog.json"
echo "Summaries: $OUTPUT_DIR/summaries/"
echo ""
echo "Done!"
