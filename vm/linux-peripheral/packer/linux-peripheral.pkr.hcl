packer {
  required_version = "= 1.16.0"

  required_plugins {
    qemu = {
      source  = "github.com/hashicorp/qemu"
      version = "= 1.1.6"
    }
  }
}

variable "base_image" {
  type = string
}

variable "base_image_sha512" {
  type = string
}

variable "instance_id" {
  type = string
}

variable "image_fingerprint" {
  type = string
}

variable "output_directory" {
  type = string
}

variable "pixi_archive" {
  type = string
}

variable "pixi_sha256" {
  type = string
}

variable "pixi_version" {
  type = string
}

variable "qemu_binary" {
  type = string
}

variable "serial_log" {
  type = string
}

variable "ssh_private_key_file" {
  type = string
}

variable "ssh_public_key_file" {
  type = string
}

source "qemu" "linux_peripheral" {
  accelerator    = "tcg"
  cpus           = 2
  disable_vnc    = true
  disk_image     = true
  disk_interface = "virtio"
  disk_size      = "12G"
  floppy_content = {
    "meta-data" = templatefile("${path.root}/cloud-init-meta-data.pkrtpl.hcl", {
      instance_id = var.instance_id
    })
    "user-data" = templatefile("${path.root}/cloud-init-user-data.pkrtpl.hcl", {
      ssh_public_key = trimspace(file(var.ssh_public_key_file))
    })
  }
  floppy_label           = "CIDATA"
  format                 = "qcow2"
  headless               = true
  iso_checksum           = "sha512:${var.base_image_sha512}"
  iso_url                = var.base_image
  machine_type           = "q35"
  memory                 = 2048
  net_device             = "virtio-net-pci"
  output_directory       = var.output_directory
  qemu_binary            = var.qemu_binary
  shutdown_command       = "sudo shutdown -P now"
  shutdown_timeout       = "5m"
  skip_compaction        = true
  ssh_private_key_file   = var.ssh_private_key_file
  ssh_read_write_timeout = "30m"
  ssh_timeout            = "30m"
  ssh_username           = "blehub"
  use_backing_file       = true
  vm_name                = "provisioned.qcow2"

  qemuargs = [
    ["-device", "qemu-xhci,id=xhci"],
    ["-no-reboot", ""],
    ["-serial", "file:${var.serial_log}"],
  ]
}

build {
  sources = ["source.qemu.linux_peripheral"]

  provisioner "shell" {
    inline = [
      "sudo cloud-init status --wait --long",
      "mkdir -p /tmp/blehub-provision",
    ]
  }

  provisioner "file" {
    source      = var.pixi_archive
    destination = "/tmp/blehub-provision/pixi.tar.gz"
  }

  provisioner "file" {
    sources = [
      "${path.root}/../provision/provision.sh",
      "${path.root}/../provision/blehub-peripheral.service",
    ]
    destination = "/tmp/blehub-provision/"
  }

  provisioner "shell" {
    environment_vars = [
      "BLEHUB_IMAGE_FINGERPRINT=${var.image_fingerprint}",
      "BLEHUB_PIXI_VERSION=${var.pixi_version}",
      "BLEHUB_PIXI_SHA256=${var.pixi_sha256}",
    ]
    inline = [
      "sudo install -d -m 0755 /opt/blehub/provision",
      "sudo install -m 0755 /tmp/blehub-provision/provision.sh /opt/blehub/provision/provision.sh",
      "sudo sed -i 's/\\r$//' /opt/blehub/provision/provision.sh",
      "sudo install -m 0644 /tmp/blehub-provision/blehub-peripheral.service /opt/blehub/provision/blehub-peripheral.service",
      "sudo --preserve-env=BLEHUB_IMAGE_FINGERPRINT,BLEHUB_PIXI_VERSION,BLEHUB_PIXI_SHA256 /opt/blehub/provision/provision.sh",
      "sudo cat /var/lib/blehub/provisioned.json",
    ]
  }
}
