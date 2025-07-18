# ansible-quasarlab 🚀

An opinionated, minimal Ansible repo to stand up a 3‑node on‑prem Kubernetes cluster (control-plane + worker on each node) with MetalLB, Argo CD, and external NGINX load balancers.

---

## What You Need to Run This

- **Ansible 2.14+** (install via `pip install ansible` or your distro’s package manager)  
- **Python 3.8+** (for Ansible)  
- **SSH access** to all target hosts, with a user that can `sudo` without a password (set `ansible_user` in `group_vars/all.yml`)  
- **Inventory file** (`inventory.ini`) listing your Kubernetes nodes and load‑balancer VMs  
- **Git access** to your cluster config repo (for Argo CD’s GitOps)  

---

## Directory Layout
ansible-quasarlab/
├── inventory.ini         # Host list & group definitions
├── group_vars/
│   └── all.yml           # Global vars: SSH user, CIDRs, versions
├── playbooks/
│   ├── k8s_cluster.yml   # Bootstraps the Kubernetes control‑plane + workers
│   └── lb_setup.yml      # Configures external NGINX load balancers
└── roles/
    ├── k8s/              # All Kubernetes‑related roles
    │   ├── common/       # OS prep: swap off, sysctl, time sync
    │   ├── containerd/   # Install & configure containerd runtime
    │   ├── kube_pkg/     # Install kubeadm, kubelet, kubectl
    │   ├── kube_init/    # `kubeadm init` on the first node
    │   ├── kube_join/    # `kubeadm join` on additional nodes
    │   ├── cni/          # Deploy Calico CNI
    │   ├── metallb/      # GitOps Application manifest for MetalLB
    │   └── argocd/       # Install Argo CD and bootstrap apps
    └── lb/               # External load‑balancer roles
        └── nginx_lb/     # Install & configure NGINX as L4 proxy

---

## Quickstart

1. **Edit** `inventory.ini` with your host IPs.  
2. **Adjust** `group_vars/all.yml` for:
   - `ansible_user`
   - `pod_network_cidr` & `service_cidr`
   - `metallb_address_pool`
   - `argocd_version`  
3. **Bootstrap** the cluster:
   ```bash
   ansible-playbook -i inventory.ini playbooks/k8s_cluster.yml
   ```
4. **Configure** the LBs::
   ```bash
   ansible-playbook -i inventory.ini playbooks/lb_setup.yml
   ```
5. 🎉 **Done!** Argo CD will watch your Git repo and deploy MetalLB (and any other apps).


## Tips & Tricks

 - Run with `--check` for a dry‑run.

 - Target individual roles via `--tags` (e.g. `--tags kube_pkg`).

 - Add or override vars in `group_vars/` or `host_vars/` as needed.

© 2025 ansible-quasarlab