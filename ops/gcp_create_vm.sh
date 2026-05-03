#!/usr/bin/env bash
set -euo pipefail

# Create a hardened Ubuntu VM on GCP for nanoclaw.
#
# What "hardened" means here:
#   - Shielded VM (Secure Boot + vTPM + Integrity Monitoring)
#   - No public SSH firewall rule. SSH only via Google IAP (35.235.240.0/20).
#     The VM is tagged `iap-ssh`; the firewall rule allows port 22 only from
#     that IAP source range.
#   - Service account scopes restricted to logging.write + monitoring.write
#     (the default compute SA otherwise gets seven scopes including pubsub
#     and devstorage.read_only that the stack doesn't need).
#
# Prerequisites (run once locally):
#   gcloud auth login
#   gcloud config set project your-gcp-project
#   gcloud services enable compute.googleapis.com iap.googleapis.com
#
# Usage:
#   bash ops/gcp_create_vm.sh [zone]
#
# Env overrides:
#   GCP_PROJECT=your-gcp-project
#   GCP_ZONE=europe-west1-b
#   VM_NAME=nanoclaw
#   MACHINE_TYPE=e2-small

GCP_PROJECT="${GCP_PROJECT:-your-gcp-project}"
GCP_ZONE="${1:-${GCP_ZONE:-europe-west1-b}}"
VM_NAME="${VM_NAME:-nanoclaw}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-small}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "Install Google Cloud SDK: https://cloud.google.com/sdk/docs/install"
  exit 1
fi

gcloud config set project "${GCP_PROJECT}"

echo "==> Creating Shielded VM ${VM_NAME} (${MACHINE_TYPE}) in ${GCP_PROJECT} zone ${GCP_ZONE}"
gcloud compute instances create "${VM_NAME}" \
  --project="${GCP_PROJECT}" \
  --zone="${GCP_ZONE}" \
  --machine-type="${MACHINE_TYPE}" \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --boot-disk-type=pd-balanced \
  --shielded-secure-boot \
  --shielded-vtpm \
  --shielded-integrity-monitoring \
  --scopes=https://www.googleapis.com/auth/logging.write,https://www.googleapis.com/auth/monitoring.write \
  --tags=iap-ssh

echo "==> Ensuring IAP-only SSH firewall rule (no public 0.0.0.0/0 SSH)"
if ! gcloud compute firewall-rules describe allow-ssh-from-iap --project="${GCP_PROJECT}" >/dev/null 2>&1; then
  gcloud compute firewall-rules create allow-ssh-from-iap \
    --project="${GCP_PROJECT}" \
    --direction=INGRESS \
    --priority=1000 \
    --network=default \
    --action=ALLOW \
    --rules=tcp:22 \
    --source-ranges=35.235.240.0/20 \
    --target-tags=iap-ssh
else
  echo "Rule allow-ssh-from-iap already exists"
fi

# Belt-and-suspenders: warn if the legacy default-allow-ssh rule (0.0.0.0/0) is still around.
if gcloud compute firewall-rules describe default-allow-ssh --project="${GCP_PROJECT}" >/dev/null 2>&1; then
  echo "WARNING: 'default-allow-ssh' (0.0.0.0/0 → tcp:22) exists in this project."
  echo "         Delete it once you have confirmed IAP SSH works:"
  echo "           gcloud compute firewall-rules delete default-allow-ssh --project=${GCP_PROJECT} -q"
fi

echo ""
echo "VM ready. SSH via IAP from your laptop:"
echo "  gcloud compute ssh ${VM_NAME} --tunnel-through-iap --zone=${GCP_ZONE} --project=${GCP_PROJECT}"
echo ""
echo "If this is your first IAP login, also grant your account the required IAM roles:"
echo "  gcloud projects add-iam-policy-binding ${GCP_PROJECT} --member=user:YOU@example.com --role=roles/iap.tunnelResourceAccessor"
echo ""
echo "Recommended next: enable OS Login project-wide (replaces metadata SSH keys with IAM):"
echo "  gcloud compute project-info add-metadata --project=${GCP_PROJECT} --metadata=enable-oslogin=TRUE"
echo "  gcloud projects add-iam-policy-binding ${GCP_PROJECT} --member=user:YOU@example.com --role=roles/compute.osLogin"
echo ""
echo "Continue setup in BOOTSTRAP.md."
