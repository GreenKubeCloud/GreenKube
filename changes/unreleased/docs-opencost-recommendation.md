### Changed

- The installation guide and README now state that OpenCost is strongly recommended. Without it and without a bundled price catalog (on-premises, kubeadm, k3s, bare metal, unknown providers), costs come from configured rates, which change measurement never trusts, so no cost saving can be verified. The setup checklist reports this case as `action_needed`; on providers with a bundled catalog, OpenCost stays optional.
