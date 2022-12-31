run:
	ansible-playbook -i inventory.yaml playbook.yaml

ping:
	ansible all -m ping -i inventory.yaml
