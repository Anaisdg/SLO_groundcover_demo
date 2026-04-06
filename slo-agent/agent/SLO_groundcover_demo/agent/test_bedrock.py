import boto3

client = boto3.client("bedrock-runtime", region_name="us-east-2")

response = client.invoke_model(
    modelId="us.anthropic.claude-opus-4-6-v1",
    body='{"anthropic_version":"bedrock-2023-05-31","max_tokens":256,"messages":[{"role":"user","content":"Hello"}]}'
)

print(response["body"].read().decode())
