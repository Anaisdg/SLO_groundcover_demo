#!/bin/bash
set -euo pipefail

# =============================================================================
# SLO Demo — Full Deploy Script
# =============================================================================
# Deploys everything needed for the SLO remediation demo:
#   1. EKS cluster (if not exists)
#   2. Storage class (gp2 as default — required by Groundcover)
#   3. Groundcover eBPF sensor
#   4. Buggy order-service
#   5. Load generator
#
# Prerequisites:
#   - AWS CLI configured with appropriate permissions
#   - eksctl installed
#   - kubectl configured
#   - Docker installed
#   - Groundcover account (sign up at https://groundcover.com)
#   - A values.yaml with your Groundcover tenant endpoint (see README)
# =============================================================================

# --- Configuration -----------------------------------------------------------
CLUSTER_NAME="${CLUSTER_NAME:-slo-demo-cluster}"
REGION="${AWS_REGION:-us-east-2}"
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

# --- Step 1: EKS Cluster -----------------------------------------------------
echo ""
echo "[1/7] Creating EKS cluster (if not exists)..."
if ! eksctl get cluster --name "${CLUSTER_NAME}" --region "${REGION}" 2>/dev/null; then
    eksctl create cluster \
        --name "${CLUSTER_NAME}" \
        --region "${REGION}" \
        --nodes 2 \
        --node-type t3.xlarge \
        --managed
    echo "  Cluster created."
else
    echo "  Cluster already exists. Skipping."
fi

# --- Step 2: Default storage class -------------------------------------------
echo ""
echo "[2/7] Setting default storage class..."
# EKS doesn't set a default storage class — Groundcover requires one
if kubectl get storageclass gp2 &>/dev/null; then
    kubectl patch storageclass gp2 -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}' 2>/dev/null || true
    echo "  gp2 set as default storage class."
else
    echo "  WARNING: gp2 storage class not found. Groundcover may fail to deploy."
    echo "  Ensure a default storage class exists before continuing."
fi

# --- Step 3: Install Groundcover CLI -----------------------------------------
echo ""
echo "[3/7] Installing Groundcover CLI..."
if ! command -v groundcover &>/dev/null; then
    sh -c "$(curl -fsSL https://groundcover.com/install.sh)"
    echo "  Groundcover CLI installed."
else
    echo "  Groundcover CLI already installed. Skipping."
fi

# --- Step 4: Deploy Groundcover eBPF sensor ----------------------------------
echo ""
echo "[4/7] Deploying Groundcover eBPF sensor..."
if [ ! -f values.yaml ]; then
    echo "  ERROR: values.yaml not found."
    echo ""
    echo "  Create one with your tenant endpoint:"
    echo ""
    echo '  cat > values.yaml << EOF'
    echo '  global:'
    echo '    backend:'
    echo '      enabled: false'
    echo '    ingress:'
    echo '      site: <your-tenant-endpoint>'
    echo '  EOF'
    echo ""
    echo "  Find your tenant endpoint in Groundcover under:"
    echo "  Data Sources > Kubernetes Clusters > CLI installation"
    exit 1
fi
groundcover deploy -f values.yaml
echo "  Waiting for Groundcover pods..."
kubectl wait --for=condition=ready pod -l app.kubernetes.io/managed-by=groundcover -n groundcover --timeout=300s 2>/dev/null || true
echo "  Groundcover deployed. Verify with: kubectl get pods -n groundcover"

# --- Step 5: ECR + Docker Build ----------------------------------------------
echo ""
echo "[5/7] Building and pushing buggy-service image..."
aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${REGION}" 2>/dev/null || \
    aws ecr create-repository --repository-name "${ECR_REPO}" --region "${REGION}"

aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

cd "$(dirname "$0")"
cd buggy-service
docker build -t "${ECR_REPO}:${IMAGE_TAG}" .
docker tag "${ECR_REPO}:${IMAGE_TAG}" "${FULL_IMAGE}"
docker push "${FULL_IMAGE}"
cd ..

# --- Step 6: Deploy to K8s ---------------------------------------------------
echo ""
echo "[6/7] Deploying order-service..."
kubectl apply -f k8s/namespace.yaml

# Substitute the image placeholder
sed "s|ORDER_SERVICE_IMAGE|${FULL_IMAGE}|g" k8s/order-service.yaml | kubectl apply -f -

echo "  Waiting for rollout..."
kubectl -n slo-demo rollout status deployment/order-service --timeout=120s

# --- Step 7: Run Load Generator ----------------------------------------------
echo ""
echo "[7/7] Starting load generator..."
echo "  Port-forwarding order-service to localhost:8000..."
kubectl -n slo-demo port-forward svc/order-service 8000:80 &
PF_PID=$!
sleep 3

echo "  Running load for 2 minutes..."
python3 load-gen/load_gen.py http://localhost:8000 --rps 2 --duration 120

kill $PF_PID 2>/dev/null || true

echo ""
echo "============================================"
echo "  Deployment complete!"
echo ""
echo "  Check Groundcover for SLO breach signals."
echo "  Then run Claude Code from this directory:"
echo "    claude"
echo "  And prompt: 'Run the SLO workflow'"
echo "============================================"
