#!/bin/bash
# Deploy TradingAgents to any K8s cluster
# Usage: ./k8s/deploy.sh [--build] [--token YOUR_GITHUB_TOKEN]

set -e

NAMESPACE="tradingagents"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Parse args
BUILD=false
TOKEN=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --build) BUILD=true; shift ;;
    --token) TOKEN="$2"; shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

echo "=== TradingAgents K8s Deployment ==="

# Build image if requested
if [ "$BUILD" = true ]; then
  echo "Building Docker image..."
  cd "$ROOT_DIR"
  docker build -t tradingagents:latest .
  echo "Image built: tradingagents:latest"
  echo ""
  echo "To push to a registry:"
  echo "  docker tag tradingagents:latest YOUR_REGISTRY/tradingagents:latest"
  echo "  docker push YOUR_REGISTRY/tradingagents:latest"
  echo "  Then update k8s manifests to use YOUR_REGISTRY/tradingagents:latest"
  echo ""
fi

# Create namespace
echo "Creating namespace..."
kubectl apply -f "$SCRIPT_DIR/namespace.yaml"

# Create or update secret
if [ -n "$TOKEN" ]; then
  echo "Creating secret with provided token..."
  kubectl -n $NAMESPACE create secret generic tradingagents-secrets \
    --from-literal=GITHUB_TOKEN="$TOKEN" \
    --dry-run=client -o yaml | kubectl apply -f -
else
  echo "Applying secret template (edit k8s/secret.yaml with your token first)..."
  kubectl apply -f "$SCRIPT_DIR/secret.yaml"
fi

# Apply config
echo "Applying configmap..."
kubectl apply -f "$SCRIPT_DIR/configmap.yaml"

# Create PVC
echo "Creating persistent volume claim..."
kubectl apply -f "$SCRIPT_DIR/pvc.yaml"

# Apply CronJob
echo "Applying daily CronJob (9:30 AM ET, Mon-Fri)..."
kubectl apply -f "$SCRIPT_DIR/cronjob.yaml"

# Apply long-lived shell pod for interactive TUI
echo "Applying long-lived ta-shell pod (for interactive TUI)..."
kubectl apply -f "$SCRIPT_DIR/pod-shell.yaml"

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Commands:"
echo "  # Interactive TUI (live Rich dashboard):"
echo "  kubectl -n $NAMESPACE exec -it ta-shell -- tradingagents"
echo ""
echo "  # Non-interactive run (script):"
echo "  kubectl -n $NAMESPACE apply -f $SCRIPT_DIR/job-manual.yaml"
echo ""
echo "  # Watch batch logs:"
echo "  kubectl -n $NAMESPACE logs -f job/tradingagents-manual"
echo ""
echo "  # Check CronJob:"
echo "  kubectl -n $NAMESPACE get cronjob"
echo ""
echo "  # List completed jobs:"
echo "  kubectl -n $NAMESPACE get jobs"
echo ""
echo "  # Edit config (symbols, models, etc.):"
echo "  kubectl -n $NAMESPACE edit configmap tradingagents-config"
echo ""
echo "  # Cleanup:"
echo "  kubectl delete namespace $NAMESPACE"
