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

# ------------------------------------------------------------------------
# Varijable
# ------------------------------------------------------------------------
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

# ------------------------------------------------------------------------
# 1. Mreža i Sigurnost
# ------------------------------------------------------------------------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = ["us-east-1a"] # Držimo sve u istoj zoni radi jednostavnosti
  }
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

# ------------------------------------------------------------------------
# 2. Odabir AMI-ja (Ubuntu 24.04)
# ------------------------------------------------------------------------
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

# ------------------------------------------------------------------------
# 3. Kreiranje Čvorova (Nodes)
# ------------------------------------------------------------------------
resource "aws_instance" "node" {
  count                  = var.node_count
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.micro"
  key_name               = var.lab_key_name
  vpc_security_group_ids = [aws_security_group.dist_sys_sg.id]
  subnet_id              = data.aws_subnets.default.ids[0]
  
  # LabInstanceProfile omogućuje instanci pisanje u CloudWatch
  # Bez ovoga boto3 u skripti neće raditi.
  iam_instance_profile   = "LabInstanceProfile"

  tags = {
    Name    = "Node-${count.index + 1}"
    Project = "P2"
    Team    = "T2" 
  }

  # Instalacija Python okruženja pri bootanju
  user_data = <<-EOF
    #!/bin/bash
  apt-get update
  apt-get install -y python3-pip python3-boto3 tmux
  EOF
}

# ------------------------------------------------------------------------
# 4. Generiranje i distribucija konfiguracije (peers.json)
# ------------------------------------------------------------------------

# Kreiramo lokalnu mapu IP adresa nakon što su instance kreirane
locals {
  peers_map = {
    for idx, instance in aws_instance.node :
    (idx + 1) => {
      ip   = instance.private_ip
      port = var.app_port
    }
  }
}

# Zapisujemo peers.json na lokalni disk
resource "local_file" "peers_json" {
  content  = jsonencode(local.peers_map)
  filename = "${path.module}/peers.json"
}

# Ovaj resurs služi za kopiranje fajlova i pokretanje aplikacije
# Pokreće se svaki put kada se promijeni ID instanci ili sadržaj peers.json
resource "terraform_data" "deploy_app" {
  count = var.node_count

  triggers_replace = [
    aws_instance.node[count.index].id,
    local_file.peers_json.content
  ]

  connection {
    type        = "ssh"
    user        = "ubuntu"
    host        = aws_instance.node[count.index].public_ip
    private_key = file("~/.ssh/labsuser.pem")
  }

  # 1. Kopiranje node.py (iz lokalnog foldera na server)
  provisioner "file" {
    source      = "node.py"
    destination = "/home/ubuntu/node.py"
  }

  # 2. Kopiranje generiranog peers.json
  provisioner "file" {
    source      = "${path.module}/peers.json"
    destination = "/home/ubuntu/peers.json"
  }

  # 3. Pokretanje aplikacije u pozadini
  provisioner "remote-exec" {
    inline = [
      "while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do sleep 1; done",
      "pkill -f node.py || true",
      "tmux has-session -t node 2>/dev/null && tmux kill-session -t node || true",
      "tmux new-session -d -s node 'USE_CLOUDWATCH=true AWS_REGION=us-east-1 python3 node.py --id ${count.index + 1} --peers peers.json | tee node.log'",
      "sleep 1"
    ]
  }
}

# ------------------------------------------------------------------------
# Outputs
# ------------------------------------------------------------------------
output "node_ips" {
  description = "Javne IP adrese svih čvorova"
  value       = { for i, node in aws_instance.node : "Node ${i+1}" => node.public_ip }
}

output "ssh_commands" {
  description = "Naredbe za spajanje na čvorove"
  value       = [for node in aws_instance.node : "ssh -i ~/.ssh/labsuser.pem ubuntu@${node.public_ip}"]
}

output "log_check_command" {
  description = "Provjeri logove na Node 1"
  value       = "ssh -i ~/.ssh/labsuser.pem ubuntu@${aws_instance.node[0].public_ip} 'tail -f node.log'"
}