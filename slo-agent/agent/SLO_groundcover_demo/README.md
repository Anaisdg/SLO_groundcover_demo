# SLO Remediation Agent Demo

**Autonomous SLO breach detection → diagnosis → Linear ticket → code patch**

Uses Groundcover MCP for observability + Linear MCP for issue tracking + AWS Bedrock (Claude Opus 4.6) for LLM reasoning.

## Architecture

```
┌──────────────┐     ┌────────────────────┐     ┌──────────────┐
│  Groundcover │────▶│  SLO Agent         │────▶│  Linear      │
│  MCP Server  │◀────│  (Bedrock Converse  │     │  MCP Server  │
│              │     │   + MCP SDK)        │     │              │
└──────────────┘     └────────────────────┘     └──────────────┘
       ▲                                               │
       │                                               ▼
┌──────────────┐                              ┌──────────────────┐
│  EKS Cluster │                              │  Linear Project  │
│  order-svc   │                              │  "SLO Demo"      │
│  (buggy)     │                              │  Marketing team  │
└──────────────┘                              └──────────────────┘
```

## Agent Flow

1. **Detect** — `groundcover_get_workloads(sortBy=p99, namespace=slo-demo)`
   → finds workloads with p99 > 500ms
2. **Diagnose** — `groundcover_query_traces(workload=order-service, sortBy=latency)`
   → identifies the slow endpoint and pattern (N+1 queries)
3. **Ticket** — `linear_create_issue(team=Marketing, project=SLO Demo)`
   → files `[SLO Breach] order-service POST /orders p99 > Xms`
4. **Patch** — outputs a suggested code fix (batch queries)

## Project Structure

```
├── buggy-service/          # The intentionally slow service
│   ├── main.py             # FastAPI app with N+1 sleep bug
│   ├── Dockerfile
│   └── requirements.txt
├── k8s/                    # Kubernetes manifests
│   ├── namespace.yaml
│   ├── order-service.yaml
│   └── slo-agent-job.yaml  # Job to run the agent in-cluster
├── load-gen/               # Traffic generator
│   └── load_gen.py
├── agent/                  # The SLO remediation agent
│   ├── slo_agent.py        # Main agent script
│   ├── Dockerfile
│   └── requirements.txt
├── deploy.sh               # Full EKS deployment script
├── run_local.sh            # Run agent locally
├── run_loadgen.sh          # Port-forward + load gen helper
├── .env.example            # Environment variable template
└── README.md
```

## Prerequisites

- AWS CLI configured + Bedrock model access (`us.anthropic.claude-opus-4-6-v1`)
- eksctl + kubectl
- Docker
- Groundcover account (MCP endpoint + API key)
- Linear workspace with Marketing team + "SLO Demo" project
- Linear API key with **full permissions** (generate at https://linear.app/settings/api)
- Python 3.11+

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

- **Groundcover**: Get your `GROUNDCOVER_API_KEY`, `GROUNDCOVER_TENANT_UUID`, and `GROUNDCOVER_BACKEND_ID` from the Groundcover dashboard under MCP setup.
- **Linear**: Generate an API key at https://linear.app/settings/api — **must have full permissions**.

### 2. Test the buggy service locally

```bash
cd buggy-service
pip install -r requirements.txt
uvicorn main:app --port 8000

# In another terminal:
cd load-gen
python load_gen.py http://localhost:8000 --rps 2 --duration 60
```

You'll see ~70% of requests breach the 500ms SLO.

### 3. Deploy to EKS

```bash
export AWS_REGION=us-east-2

chmod +x deploy.sh
./deploy.sh
```

This creates the EKS cluster, builds/pushes the image, deploys, and runs load.

### 4. Run the agent locally

```bash
cd agent
pip install -r requirements.txt

# Source your .env and run
source ../.env
python slo_agent.py
```

Or use the helper script:

```bash
chmod +x run_local.sh run_loadgen.sh

# Generate load (port-forwards automatically)
./run_loadgen.sh 60 2

# Run the agent
./run_local.sh
```

### 5. Deploy the agent as a K8s Job

Running the agent in-cluster lets Groundcover capture the agent's own pod logs.

#### Set up IRSA (IAM Role for Service Account)

```bash
# Create the IAM policy
aws iam create-policy --policy-name slo-agent-bedrock-access --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:Converse"], "Resource": "*"}]
}'

# Create the service account with IRSA
eksctl create iamserviceaccount \
  --name slo-agent \
  --namespace slo-demo \
  --cluster slo-demo-cluster \
  --region us-east-2 \
  --attach-policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/slo-agent-bedrock-access \
  --approve
```

#### Create K8s secret from .env

**Important**: `kubectl create secret --from-env-file` does NOT strip quotes. If your `.env` wraps values in quotes, strip them first:

```bash
sed 's/="\(.*\)"$/=\1/' .env > /tmp/slo-clean.env
kubectl create secret generic slo-agent-secrets -n slo-demo \
  --from-env-file=/tmp/slo-clean.env
rm /tmp/slo-clean.env
```

#### Build, push, and deploy

```bash
# Get your AWS account ID and ECR URI
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.us-east-2.amazonaws.com/slo-agent"

# Create ECR repo (if needed)
aws ecr create-repository --repository-name slo-agent --region us-east-2 2>/dev/null || true

# Build and push
aws ecr get-login-password --region us-east-2 | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.us-east-2.amazonaws.com"

cd agent
docker build --platform linux/amd64 -t "${ECR_URI}:latest" .
docker push "${ECR_URI}:latest"
cd ..

# Generate load first
./run_loadgen.sh 60 2

# Deploy the job
kubectl delete job slo-agent-run -n slo-demo --ignore-not-found
sed "s|SLO_AGENT_IMAGE|${ECR_URI}:latest|g" k8s/slo-agent-job.yaml | kubectl apply -f -

# Watch the agent run
kubectl logs -n slo-demo job/slo-agent-run -f
```

## Linear Issue Format

The agent creates issues matching this pattern:

```
Title: [SLO Breach] order-service POST /orders p99 > 1877ms (target: 500ms)
Labels: slo-breach, Bug
Priority: Urgent
Project: SLO Demo
Team: Marketing
```

With a detailed description including detection data, root cause analysis, suggested fix, and trace evidence.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-opus-4-6-v1` | Bedrock model to use |
| `AWS_REGION` | `us-east-2` | AWS region |
| `SLO_THRESHOLD_MS` | `500` | SLO target in milliseconds |
| `TARGET_NAMESPACE` | `slo-demo` | K8s namespace to monitor |
| `GROUNDCOVER_MCP_URL` | `https://mcp.groundcover.com/api/mcp` | Groundcover MCP endpoint |
| `LINEAR_MCP_URL` | `https://mcp.linear.app/sse` | Linear MCP endpoint |
| `GROUNDCOVER_API_KEY` | *(required)* | Groundcover API key (Bearer token) |
| `GROUNDCOVER_TENANT_UUID` | *(required)* | Groundcover tenant UUID |
| `GROUNDCOVER_BACKEND_ID` | *(required)* | Groundcover backend ID |
| `GROUNDCOVER_TIMEZONE` | `America/Chicago` | Timezone for Groundcover queries |
| `LINEAR_API_KEY` | *(required)* | Linear API key (full permissions) |

## MCP Transport Notes

- **Groundcover** uses Streamable HTTP transport (`streamablehttp_client`) with Bearer token + tenant headers
- **Linear** uses SSE transport (`sse_client`) at `/sse` with Bearer API key
- Linear API keys **must have full permissions** — restricted keys cause server errors
- No `mcp-remote` or Node.js dependency required
