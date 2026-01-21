# P2 - Distribuirana koordinacija (Tim T2)

Ovaj repozitorij sadrži implementaciju distribuiranog sustava na AWS platformi koja demonstrira ključne algoritme koordinacije. Projekt koristi **Terraform** za _Infrastructure as Code_ (IaC) i automatsko postavljanje okruženja.

Implementirani koncepti:

1. **Lamportovi satovi**: Logičko mjerenje vremena.
2. **Ricart-Agrawala algoritam**: Međusobno isključivanje (Mutex) za pristup kritičnoj sekciji.
3. **Bully algoritam**: Izbor vođe (Leader Election) i tolerancija na kvarove.

## Struktura tima

- **Vedran Marić**: Voditelj, integracija.
- **Anđela Marinović**: Infrastruktura i komunikacija (AWS/Terraform).
- **Leo Petrović**: Algoritmi (Mutex/Lamport).
- **Nikola Pehar**: Leader Election i mjerenja.

## Struktura projekta

```
├── src/                      # Python kod koji se izvršava na cluster čvorovima
│   ├── node.py              # Glavni kod (Lamport, Ricart-Agrawala, Bully)
│   ├── cloudwatch_logger.py # CloudWatch logging integracija
│   ├── peers.json           # Konfiguracija peer čvorova (generira Terraform)
│   ├── pyproject.toml       # Python dependencies
│   └── uv.lock              # Locked dependencies
├── terraform/               # AWS EC2 infrastruktura setup
│   ├── main.tf              # Terraform konfiguracija
│   └── user_data.sh.tpl     # Bootstrap skripta za EC2 instance
├── scripts/                 # Admin skripte
│   ├── admin_script.sh      # Remote komande za čvorove
│   └── deploy.sh            # Deploy skripta za čvor
└── README.md                # Ovaj file
```

## Preduvjeti

- **AWS Academy Learner Lab** pristup (aktiviran _Lab_).
- **Terraform** instaliran lokalno.
- **AWS CLI** konfiguriran s `aws_access_key_id` i `aws_secret_access_key` iz _AWS Details_ panela.

## Arhitektura i Automatizacija

Sustav se sastoji od 5 EC2 instanci unutar VPC-a.

Cijeli deploy proces je automatiziran putem main.tf datoteke koja:

1. Kreira mrežnu infrastrukturu (VPC, Subnet, Security Groups).
2. Postavlja IAM role za pisanje u **CloudWatch**.
3. Kreira EC2 instance s fiksiranim privatnim IP adresama.
4. Putem `user_data` skripte automatski:
   - Instalira Python i biblioteke.
   - Generira `peers.json`.
   - Pokreće `node.py` u pozadini u **automatskom simulacijskom modu**.

## Upute za pokretanje (AWS)

### 1. Podizanje infrastrukture (Deploy)

Pozicionirajte se u **terraform** direktorij i pokrenite:

Bash

```
cd terraform
terraform init
terraform apply -auto-approve
```

Pričekajte 2-3 minute da se instance podignu i da inicijalizacijske skripte završe instalaciju. Nije potrebna nikakva ručna intervencija.

### 2. Monitoring (CloudWatch)

Prema specifikaciji projekta, svi logovi se šalju na AWS CloudWatch.

1. Otvorite AWS Konzolu -> **CloudWatch**.
2. U lijevom izborniku odaberite **Logs** -> **Log groups**.
3. Otvorite grupu: `/Distributed_System_Logs`.
4. Vidjet ćete _Log streamove_ za svaki čvor (`Node_1`, `Node_2`, ...).
5. Klikom na stream možete pratiti:
   - Promjene Lamportovog sata.
   - Zahtjeve za kritičnu sekciju (ENTER/EXIT).
   - Promjene vođe (LEADER_UPDATE).

### 3. Demo scenariji i testiranje

Sustav je konfiguriran da automatski simulira rad (slanje poruka i zahtjeva). Za ručno testiranje specifičnih scenarija (npr. rušenje vođe):

1. Spojite se na instancu putem SSH (IP adrese su ispisane u Terraform outputu):

   Bash

   ```
   ssh -i vockey.pem ec2-user@<JAVNA-IP-ADRESA>
   ```

2. Provjerite status procesa:

   Bash

   ```
   ps aux | grep node.py
   ```

3. **Simulacija kvara:** Ubijte proces trenutnog vođe:

   Bash

   ```
   kill <PID>
   ```

4. Pratite CloudWatch logove ostalih čvorova – trebali bi detektirati kvar i izabrati novog vođu.

## Lokalno pokretanje (Development)

Za potrebe razvoja i testiranja bez AWS-a:

1. Uredite `src/node.py` i postavite `USE_CLOUDWATCH = False` (ili koristite default).
2. Kreirajte lokalni `src/peers.json` (localhost portovi 5001-5005).
3. Pokrenite čvorove u zasebnim terminalima:

   Bash

   ```
   cd src
   python3 node.py --id 1 --peers peers.json
   python3 node.py --id 2 --peers peers.json
   # ...
   ```

4. U lokalnom načinu rada, čvorovi prihvaćaju komande putem tipkovnice (`req`, `elect`, `status`).

## Čišćenje (Cleanup)

Kako ne bi trošili AWS kredit, nakon završetka rada obavezno uklonite resurse:

Bash

```
cd terraform
terraform destroy -auto-approve
```
