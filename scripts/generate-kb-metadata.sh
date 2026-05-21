#!/usr/bin/env bash
# generate-kb-metadata.sh — Creates .metadata.json sidecar files for Bedrock KB
#
# Bedrock Knowledge Base uses these sidecar files to populate metadata fields
# that enable filtered retrieval (use_case, deployment_type, document_type).
#
# Run this BEFORE uploading to S3:
#   ./scripts/generate-kb-metadata.sh
#   aws s3 sync knowledge-base/ s3://YOUR-KB-BUCKET/
#
# The sidecar file must be named exactly: {original_filename}.metadata.json
# and placed in the same S3 prefix as the source document.
#
# Reference: https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-ds-metadata.html

set -euo pipefail

KB_DIR="${1:-knowledge-base}"

if [ ! -d "$KB_DIR" ]; then
  echo "ERROR: Knowledge base directory not found: $KB_DIR" >&2
  exit 1
fi

count=0

find "$KB_DIR" -type f \( -name "*.md" -o -name "*.txt" -o -name "*.json" -o -name "*.yaml" \) | while read -r filepath; do
  # Skip existing metadata files
  if [[ "$filepath" == *.metadata.json ]]; then
    continue
  fi

  # Extract metadata from path: kb_dir/use_case/deployment_type/document_type.ext
  # OR: kb_dir/use_case/document_type.ext (for best-practices at use_case level)
  rel_path="${filepath#$KB_DIR/}"

  IFS='/' read -ra parts <<< "$rel_path"
  num_parts=${#parts[@]}

  if [ "$num_parts" -ge 3 ]; then
    # Standard: use_case/deployment_type/document_type.ext
    use_case="${parts[0]}"
    deployment_type="${parts[1]}"
    filename="${parts[2]}"
    document_type="${filename%.*}"
  elif [ "$num_parts" -eq 2 ]; then
    # Top-level use_case doc: use_case/document_type.ext
    use_case="${parts[0]}"
    deployment_type=""
    filename="${parts[1]}"
    document_type="${filename%.*}"
  else
    echo "SKIP: Cannot extract metadata from path: $filepath" >&2
    continue
  fi

  # Build metadata JSON
  metadata_file="${filepath}.metadata.json"

  if [ -n "$deployment_type" ]; then
    cat > "$metadata_file" <<EOF
{
  "metadataAttributes": {
    "use_case": "${use_case}",
    "deployment_type": "${deployment_type}",
    "document_type": "${document_type}"
  }
}
EOF
  else
    cat > "$metadata_file" <<EOF
{
  "metadataAttributes": {
    "use_case": "${use_case}",
    "document_type": "${document_type}"
  }
}
EOF
  fi

  count=$((count + 1))
  echo "  Created: $metadata_file"
done

echo ""
echo "Done. Generated metadata sidecar files."
echo ""
echo "Next steps:"
echo "  1. Upload to S3:  aws s3 sync $KB_DIR/ s3://YOUR-KB-BUCKET/"
echo "  2. Sync data source in Bedrock console (or via API)"
echo "  3. Verify with:  aws bedrock-agent-runtime retrieve --knowledge-base-id YOUR-KB-ID --retrieval-query '{\"text\": \"test\"}'"
