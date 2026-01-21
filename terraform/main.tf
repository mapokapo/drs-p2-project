terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "us-east-1"
}

# ========================================
# Variables
# ========================================

variable "lab_key_name" {
  description = "Naziv postojećeg ključa u AWS Learner Lab"
  default     = "vockey"
}

variable "node_count" {
  description = "Broj čvorova u distribuiranom sustavu (Min 5 prema zahtjevima)"
  default     = 5 
}

variable "app_port" {
  description = "TCP port na kojem node.py sluša"
  default     = 5000
}

variable "iam_instance_profile" {
  description = "IAM Instance Profile to attach to nodes"
  default     = "LabInstanceProfile"
}

# ========================================
# Network & Security
# ========================================

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  # Removed availability-zone filter to let AWS pick any available subnet in the VPC
}

resource "aws_security_group" "dist_sys_sg" {
  name        = "dist-system-sg"
  description = "Allow SSH and internal app traffic"
  vpc_id      = data.aws_vpc.default.id

  # SSH pristup
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Interna komunikacija aplikacije (TCP 5000)
  ingress {
    from_port = var.app_port
    to_port   = var.app_port
    protocol  = "tcp"
    self      = true # Dozvoli promet samo između instanci u ovoj grupi
  }

  # Outbound internet (potrebno za CloudWatch i instalaciju paketa)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ========================================
# AMI Selection (Ubuntu 24.04)
# ========================================

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

# ========================================
# Node Resources
# ========================================

resource "aws_instance" "distributed_node" {
  count                  = var.node_count
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.micro"
  key_name               = var.lab_key_name
  vpc_security_group_ids = [aws_security_group.dist_sys_sg.id]
  # Pick a subnet automatically from the available ones
  subnet_id              = element(data.aws_subnets.default.ids, count.index % length(data.aws_subnets.default.ids))
  
  iam_instance_profile   = var.iam_instance_profile

  tags = {
    Name    = "Node-${count.index + 1}"
    Project = "P2"
    Team    = "T2" 
  }

  user_data = templatefile("${path.module}/user_data.sh.tpl", {})
}

# ========================================
# Configuration & Deployment
# ========================================

# Kreiramo lokalnu mapu IP adresa nakon što su instance kreirane
locals {
  peers_map = {
    for idx, instance in aws_instance.distributed_node :
    (idx + 1) => {
      ip   = instance.private_ip
      port = var.app_port
    }
  }
}

# Zapisujemo peers.json na lokalni disk
resource "local_file" "peers_json" {
  content  = jsonencode(local.peers_map)
  filename = "${path.module}/../src/peers.json"
}

# Ovaj resurs služi za kopiranje fajlova i pokretanje aplikacije
resource "terraform_data" "node_deployment" {
  count = var.node_count

  triggers_replace = [
    aws_instance.distributed_node[count.index].id,
    local_file.peers_json.content,
    filesha1("${path.module}/../scripts/deploy.sh"),
    filesha1("${path.module}/../src/node.py")
  ]

  connection {
    type        = "ssh"
    user        = "ubuntu"
    host        = aws_instance.distributed_node[count.index].public_ip
    private_key = file("~/.ssh/labsuser.pem")
  }

  # 1. Kopiranje node.py (iz lokalnog foldera na server)
  provisioner "file" {
    source      = "${path.module}/../src/node.py"
    destination = "/home/ubuntu/node.py"
  }

  # 2. Kopiranje cloudwatch_logger.py
  provisioner "file" {
    source      = "${path.module}/../src/cloudwatch_logger.py"
    destination = "/home/ubuntu/cloudwatch_logger.py"
  }

  # 3. Kopiranje generiranog peers.json
  provisioner "file" {
    source      = "${path.module}/../src/peers.json"
    destination = "/home/ubuntu/peers.json"
  }

  # 4. Kopiranje deploy skripte
  provisioner "file" {
    source      = "${path.module}/../scripts/deploy.sh"
    destination = "/home/ubuntu/deploy.sh"
  }

  # 5. Pokretanje aplikacije putem deploy skripte
  provisioner "remote-exec" {
    inline = [
      "chmod +x /home/ubuntu/deploy.sh",
      "/home/ubuntu/deploy.sh ${count.index + 1}"
    ]
  }
}

# ========================================
# Outputs
# ========================================

output "node_ips" {
  description = "Javne IP adrese svih čvorova"
  value       = { for i, node in aws_instance.distributed_node : "Node ${i+1}" => node.public_ip }
}

output "ssh_quickstart" {
  description = "Naredba za spajanje na prvi čvor radi provjere"
  value       = "ssh -i ~/.ssh/labsuser.pem ubuntu@${aws_instance.distributed_node[0].public_ip}"
}