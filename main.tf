terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = "us-east-1" # Provjerite je li ovo vaša Learner Lab regija
}

# --- 1. MREŽA (VPC & Subnet) ---
resource "aws_vpc" "dist_vpc" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags = { Name = "P2_Dist_System_VPC" }
}

resource "aws_internet_gateway" "gw" {
  vpc_id = aws_vpc.dist_vpc.id
}

resource "aws_route_table" "rt" {
  vpc_id = aws_vpc.dist_vpc.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gw.id
  }
}

resource "aws_subnet" "main_subnet" {
  vpc_id                  = aws_vpc.dist_vpc.id
  cidr_block              = "10.0.1.0/24"
  map_public_ip_on_launch = true # Svaka instanca treba javni IP za SSH pristup
  tags = { Name = "P2_Subnet" }
}

resource "aws_route_table_association" "a" {
  subnet_id      = aws_subnet.main_subnet.id
  route_table_id = aws_route_table.rt.id
}

# --- 2. SIGURNOST (Security Groups) ---
resource "aws_security_group" "allow_p2_traffic" {
  name        = "allow_p2_traffic"
  description = "Allow inbound traffic for P2 project"
  vpc_id      = aws_vpc.dist_vpc.id

  # SSH pristup (opcionalno ograničiti na vaš IP u produkciji)
  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # TCP portovi za Node komunikaciju (5000-5010)
  ingress {
    from_port   = 5000
    to_port     = 5010
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"] # Samo unutar VPC-a
  }

  # Egress - dozvoli sve (potrebno za pip install i CloudWatch)
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# --- 3. LOGGING & IAM (CloudWatch) ---
# Kreiramo Log grupu unaprijed kako bi je čvorovi mogli koristiti
resource "aws_cloudwatch_log_group" "dist_logs" {
  name              = "Distributed_System_Logs"
  retention_in_days = 1
}

# IAM Rola da instance mogu pisati u CloudWatch
resource "aws_iam_role" "ec2_role" {
  name = "P2_EC2_CloudWatch_Role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "cw_policy" {
  name = "P2_CloudWatch_Policy"
  role = aws_iam_role.ec2_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogStreams"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "p2_profile" {
  name = "P2_Instance_Profile"
  role = aws_iam_role.ec2_role.name
}

# --- 4. INSTANCE (EC2) ---
resource "aws_instance" "nodes" {
  count         = 5
  ami           = "ami-0c7217cdde317cfec" # Amazon Linux 2023 (US-EAST-1). Promijenite ako ste u drugoj regiji!
  instance_type = "t2.micro" 
  subnet_id     = aws_subnet.main_subnet.id
  vpc_security_group_ids = [aws_security_group.allow_p2_traffic.id]
  iam_instance_profile   = aws_iam_instance_profile.p2_profile.name

  # Fiksiramo privatne IP adrese kako bi "peers.json" bio jednostavan
  private_ip = "10.0.1.1${count.index + 1}" # Kreira 10.0.1.11, 10.0.1.12...

  tags = {
    Name    = "Node-${count.index + 1}"
    Project = "P2"
    Team    = "T2"
  }

  # User Data skripta koja se vrti pri prvom bootu
  user_data = <<-EOF
              #!/bin/bash
              # 1. Priprema okruženja
              yum update -y
              yum install -y python3-pip
              pip3 install boto3

              # 2. Postavljanje radnog direktorija
              mkdir -p /home/ec2-user/app
              cd /home/ec2-user/app

              # 3. Kreiranje peers.json (Hardkodiramo jer znamo IP-ove iz Terraforma)
              cat <<EOT > peers.json
              {
                  "1": {"ip": "10.0.1.11", "port": 5001},
                  "2": {"ip": "10.0.1.12", "port": 5002},
                  "3": {"ip": "10.0.1.13", "port": 5003},
                  "4": {"ip": "10.0.1.14", "port": 5004},
                  "5": {"ip": "10.0.1.15", "port": 5005}
              }
              EOT

              # 4. Kreiranje node.py (Kopiramo sadržaj Python skripte ovdje)
              # (Napomena: Terraform će ovdje ubaciti sadržaj datoteke ako koristite templatefile,
              # ali za ovaj primjer pretpostavljamo da korisnik ručno kopira kod ili koristi s3.
              # Ovdje koristimo wget s gist-a ili slično u praksi, ali za sada samo dummy echo
              # da simuliramo postojanje datoteke. U stvarnosti, OVDJE TREBA BITI PYTHON KOD)
              
              # *** OVDJE INSERTIRAJTE PYTHON KOD IZ KORAKA 1 ***
              # Zbog limita znakova, ovo radite ručno ili koristite 'local-exec' provisioner.
              # Za potrebe primjera, pretpostavit ćemo da ste uploadali node.py na S3 ili ga pasteate:
              
              cat <<PYend > node.py
              $(cat ${path.module}/node.py)
              PYend

              # 5. Pokretanje aplikacije
              # Node ID određujemo na temelju zadnjeg broja IP adrese (11 -> 1, 12 -> 2...)
              MY_IP=$(hostname -I | awk '{print $1}')
              NODE_ID=$(( ''${MY_IP##*.}'' - 10 ))

              export USE_CLOUDWATCH=true
              export AWS_REGION=us-east-1
              export AUTO_RUN=true

              # Pokrećemo u pozadini
              nohup python3 node.py --id $NODE_ID --peers peers.json > node.log 2>&1 &
              EOF
}

# --- 5. OUTPUTS ---
output "node_public_ips" {
  value = { for i, instance in aws_instance.nodes : "Node ${i+1}" => instance.public_ip }
}