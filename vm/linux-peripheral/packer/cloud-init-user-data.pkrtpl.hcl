#cloud-config
hostname: blehub-peripheral
manage_etc_hosts: true
ssh_pwauth: false
disable_root: true
users:
  - name: blehub
    gecos: BleHub Peripheral Fixture
    groups: [adm, sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys:
      - ${ssh_public_key}
final_message: "BleHub Linux Peripheral image bootstrap complete"
