# SLO Remediation Agent Demo

**Autonomous SLO breach detection → diagnosis → Linear ticket → code patch**

Uses Groundcover MCP for observability + Linear MCP for issue tracking + AWS Bedrock for LLM reasoning.

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
slo-agent/
├── buggy-service/          # Phase 1: The intentionally slow service
│   ├── main.py             # FastAPI app with N+1 sleep bug
│   ├── Dockerfile
│   └── requirements.txt
├── k8s/                    # Phase 2: Kubernetes manifests
│   ├── namespace.yaml
│   ├── order-service.yaml
│   └── slo-agent-job.yaml  # Optional: Job to run the agent in-cluster (GC logs)
├── load-gen/               # Phase 2: Traffic generator
│   └── load_gen.py
├── agent/                  # Phase 3: The SLO remediation agent
│   ├── slo_agent.py        # Main agent script
│   ├── Dockerfile          # Optional: run agent in EKS so GC captures pod logs
│   └── requirements.txt
├── deploy.sh               # Full deployment script
└── README.md
```

## Prerequisites

- AWS CLI configured + Bedrock model access (`anthropic.claude-sonnet-4-20250514-v1:0`)
- eksctl + kubectl
- Docker
- Groundcover account (MCP endpoint)
- Linear workspace with Marketing team + "SLO Demo" project
- Python 3.11+

## Quick Start

### Phase 1: Test locally

```bash
cd slo-agent/buggy-service
pip install -r requirements.txt
uvicorn main:app --port 8000

# In another terminal:
cd ../load-gen
python load_gen.py http://localhost:8000 --rps 2 --duration 60
```

You'll see ~70% of requests breach the 500ms SLO.

### Phase 2: Deploy to EKS

```bash
cd slo-agent
# Set your region
export AWS_REGION=us-east-1

# Run the full deployment
chmod +x deploy.sh
./deploy.sh
```

This creates the EKS cluster, builds/pushes the image, deploys, and runs load.

### Phase 3: Run the agent

```bash
cd slo-agent/agent
pip install -r requirements.txt

# Set MCP credentials
export GROUNDCOVER_MCP_URL="https://mcp.groundcover.com/sse"
export LINEAR_MCP_URL="https://mcp.linear.app/mcp"
export GROUNDCOVER_API_KEY="your-key"
export LINEAR_API_KEY="your-key"

# Optional: override defaults
export TARGET_NAMESPACE="slo-demo"
export SLO_THRESHOLD_MS="500"

python slo_agent.py
```

### Capturing agent logs in Groundcover

Groundcover (and most cluster log pipelines) collect **workloads running in Kubernetes**, not processes on your laptop. To see **this agent’s** stdout/stderr in Groundcover alongside `order-service`:

1. Run the agent as a **pod** in the same cluster (for example the included **`k8s/slo-agent-job.yaml`** Job in `slo-demo`).
2. Ensure **Groundcover monitors** the `slo-demo` namespace (same as where `order-service` runs).
3. Give the pod **AWS credentials for Bedrock** (recommended: **IRSA** — IAM Role for Service Account; acceptable for a demo: keys in a Kubernetes Secret) plus **MCP-related** secrets (`GROUNDCOVER_MCP_URL`, `LINEAR_MCP_URL`, API keys, and any Groundcover headers your tenant requires).

Create the secret (example — adjust keys to match `.env.example` and your org):

```bash
kubectl create secret generic slo-agent-secrets -n slo-demo \
  --from-literal=AWS_REGION=us-east-1 \
  --from-literal=AWS_ACCESS_KEY_ID=... \
  --from-literal=AWS_SECRET_ACCESS_KEY=... \
  --from-literal=GROUNDCOVER_MCP_URL=https://mcp.groundcover.com/sse \
  --from-literal=LINEAR_MCP_URL=https://mcp.linear.app/mcp \
  --from-literal=GROUNDCOVER_API_KEY=... \
  --from-literal=LINEAR_API_KEY=... \
  --from-literal=TARGET_NAMESPACE=slo-demo \
  --from-literal=SLO_THRESHOLD_MS=500
```

Build and push the agent image (same ECR login pattern as `deploy.sh`), then substitute the image in the Job manifest and apply:

```bash
cd agent
docker build -t slo-agent:latest .
# tag/push to ECR, then:
kubectl delete job slo-agent-run -n slo-demo --ignore-not-found
sed "s|SLO_AGENT_IMAGE|${YOUR_ECR_URI}|g" ../k8s/slo-agent-job.yaml | kubectl apply -f -
kubectl logs -n slo-demo job/slo-agent-run -f
```

For **production-style** AWS auth in the pod, replace static keys with **IRSA** and omit `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` from the secret.

The agent will:
1. Query Groundcover → detect `order-service` breaching p99 > 500ms
2. Pull traces → see the N+1 pattern on `POST /orders`
3. File a Linear issue in Marketing / SLO Demo
4. Output a code patch (batched DB queries)

## Linear Issue Format

The agent creates issues matching this pattern:

```
Title: [SLO Breach] order-service POST /orders p99 > 2850ms (target: 500ms)
Labels: slo-breach, Bug
Priority: Urgent
Project: SLO Demo
Team: Marketing
```

With a detailed description including detection data, root cause analysis, suggested fix, and trace evidence.

## Environment Variables

| Variable              | Default                              | Description                  |
|-----------------------|--------------------------------------|------------------------------|
| `BEDROCK_MODEL_ID`    | `anthropic.claude-sonnet-4-20250514-v1:0` | Bedrock model to use         |
| `AWS_REGION`          | `us-east-1`                          | AWS region                   |
| `SLO_THRESHOLD_MS`    | `500`                                | SLO target in milliseconds   |
| `TARGET_NAMESPACE`    | `slo-demo`                           | K8s namespace to monitor     |
| `GROUNDCOVER_MCP_URL` | `https://mcp.groundcover.com/sse`    | Groundcover MCP endpoint     |
| `LINEAR_MCP_URL`      | `https://mcp.linear.app/mcp`         | Linear MCP endpoint          |
| `GROUNDCOVER_API_KEY` | *(empty)*                            | Groundcover auth token       |
| `LINEAR_API_KEY`      | *(empty)*                            | Linear auth token            |
