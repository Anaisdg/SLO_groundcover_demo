#!/bin/bash
set -euo pipefail

# =============================================================================
# SLO Demo — Deploy Script
# =============================================================================
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - eksctl installed
#   - kubectl configured
#   - Docker installed
#   - Groundcover account + API key
#   - Linear API key
# =============================================================================

# --- Configuration -----------------------------------------------------------
CLUSTER_NAME="${CLUSTER_NAME:-slo-demo-cluster}"
REGION="${AWS_REGION:-us-east-1}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="order-service"
IMAGE_TAG="latest"
FULL_IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}"

echo "============================================"
echo "  SLO Demo Deployment"
echo "  Cluster: ${CLUSTER_NAME}"
echo "  Region:  ${REGION}"
echo "  Image:   ${FULL_IMAGE}"
echo "============================================"

# --- Phase 2a: EKS Cluster --------------------------------------------------
echo ""
echo "[1/6] Creating EKS cluster (if not exists)..."
if ! eksctl get cluster --name "${CLUSTER_NAME}" --region "${REGION}" 2>/dev/null; then
    eksctl create cluster \
        --name "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --nodes 2 \
        --node-type t3.medium \
        --managed
    echo "  Cluster created."
else
    echo "  Cluster already exists. Skipping."
fi

# --- Phase 2b: ECR + Docker Build -------------------------------------------
echo ""
echo "[2/6] Creating ECR repository (if not exists)..."
aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${REGION}" 2>/dev/null || \
    aws ecr create-repository --repository-name "${ECR_REPO}" --region "${REGION}"

echo ""
echo "[3/6] Building and pushing Docker image..."
aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

cd "$(dirname "$0")"
cd buggy-service
docker build -t "${ECR_REPO}:${IMAGE_TAG}" .
docker tag "${ECR_REPO}:${IMAGE_TAG}" "${FULL_IMAGE}"
docker push "${FULL_IMAGE}"
cd ..

# --- Phase 2c: Deploy to K8s ------------------------------------------------
echo ""
echo "[4/6] Deploying to Kubernetes..."
kubectl apply -f k8s/namespace.yaml

# Substitute the image placeholder
sed "s|ORDER_SERVICE_IMAGE|${FULL_IMAGE}|g" k8s/order-service.yaml | kubectl apply -f -

echo "  Waiting for rollout..."
kubectl -n slo-demo rollout status deployment/order-service --timeout=120s

# --- Phase 2d: Install Groundcover ------------------------------------------
echo ""
echo "[5/6] Installing Groundcover..."
echo "  If not already installed, run:"
echo "    curl -fsSL https://app.groundcover.com/install | bash"
echo "  Then verify with: kubectl get pods -n groundcover"
echo ""

# --- Phase 2e: Run Load Generator -------------------------------------------
echo ""
echo "[6/6] Starting load generator..."
echo "  Port-forwarding order-service to localhost:8000..."
kubectl -n slo-demo port-forward svc/order-service 8000:80 &
PF_PID=$!
sleep 3

echo "  Running load for 2 minutes..."
python3 load-gen/load_gen.py http://localhost:8000 --rps 2 --duration 120

kill $PF_PID 2>/dev/null || true

echo ""
echo "============================================"
echo "  Load generation complete."
echo "  Check Groundcover for SLO breach signals."
echo "  Then run the agent:"
echo "    cd agent && python slo_agent.py"
echo "============================================"
