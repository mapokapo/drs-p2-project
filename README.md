# P2 - Distribuirana koordinacija (Tim T2)

**Studij:** Diplomski studij računarstva  
**AWS Academy Class:** P2 - Distribuirani računalni sustavi  
**Veličina tima:** 4 člana

---

## Sažetak projekta

Ovaj repozitorij sadrži **kompletan distribuirani sustav** implementiran na AWS platformi koji demonstrira ključne algoritme koordinacije u distribuiranim sustavima. Projekt u potpunosti zadovoljava zahtjeve projektnog zadatka P2 i koristi **Terraform** za _Infrastructure as Code_ (IaC) s potpuno automatiziranim postavljanjem okruženja.

### Implementirani algoritmi i koncepti:

1. **Lamportovi logički satovi** - Logičko mjerenje vremena bez globalnog sata
2. **Ricart-Agrawala algoritam** - Međusobno isključivanje (Mutex) za pristup kritičnoj sekciji
3. **Bully algoritam** - Izbor vođe (Leader Election) i automatski oporavak od kvara

### Ispunjeni minimalni zahtjevi:

- [x] **5 čvorova** - Svaki čvor s jedinstvenim identitetom
- [x] **Lamportov sat** - Ispravna implementacija `max + 1` pravila
- [x] **Međusobno isključivanje** - Dokazano u CloudWatch logovima
- [x] **Izbor vođe** - Automatski oporavak od kvara vođe (heartbeat + timeout)
- [x] **Mjerenja performansi** - Broj poruka i vrijeme čekanja
- [x] **CloudWatch** - Strukturirani logovi i alarmi
- [x] **IAM** - Least-privilege princip (LabInstanceProfile)
- [x] **Tagiranje** - `Project=P2` i `Team=T2`

---

## Struktura tima

- **Vedran Marić** - Voditelj projekta, integracija, dokumentacija i priprema demo-a
- **Anđela Marinović** - Komunikacija i infrastruktura (AWS/Terraform, bootstrap)
- **Leo Petrović** - Logičko vrijeme i međusobno isključivanje (Lamport sat, Ricart-Agrawala)
- **Nikola Pehar** - Izbor vođe i mjerenja (Bully algoritam, eksperimenti, analiza)

---

## Struktura projekta

```
├── src/                      # Python kod čvorova
│   ├── node.py              # Glavna implementacija (Lamport, Ricart-Agrawala, Bully)
│   ├── cloudwatch_logger.py # CloudWatch logging integracija
│   ├── peers.json           # Konfiguracija peer mreže (generira Terraform za AWS)
│   ├── pyproject.toml       # Python dependencies (uv format)
│   └── uv.lock              # Locked dependencies
├── benchmark/               # Mjerenje performansi
│   ├── benchmark.py         # Skripta za automatsko mjerenje performansi
│   ├── peers_3nodes.json    # 3-node konfiguracija za benchmark
│   ├── peers_5nodes.json    # 5-node konfiguracija za benchmark
│   └── peers_7nodes.json    # 7-node konfiguracija za benchmark
├── terraform/               # AWS infrastruktura (IaC)
│   ├── main.tf              # Terraform konfiguracija (VPC, EC2, IAM, deploy)
│   └── user_data.sh.tpl     # Bootstrap skripta za EC2 instance
├── scripts/                 # Admin i deploy skripte
│   ├── admin_script.sh      # Slanje komandi na remote čvorove via tmux
│   └── deploy.sh            # Deploy i pokretanje čvora (poziva Terraform)
├── docs/
│   └── architecture.md      # Arhitekturni dijagram (Mermaid format)
├── .gitignore               # Git ignore pravila
└── README.md                # Ovaj dokument

```

---

## Arhitektura sustava

> **Vizualni dijagrami:** Detaljna arhitektura s Mermaid dijagramima dostupna je u [`docs/architecture.md`](docs/architecture.md)

### Infrastruktura (AWS)

Sustav se sastoji od **5 EC2 instanci** (t3.micro, Ubuntu 24.04) unutar default VPC-a:

- **VPC i Subnet** - Koristi default VPC s automatskim izborom subneta
- **Security Group** - Dozvoljava SSH (port 22) i inter-node TCP komunikaciju (port 5000)
- **IAM Instance Profile** - `LabInstanceProfile` za CloudWatch pristup
- **CloudWatch Logs** - Log grupa `/Distributed_System_Logs` sa streamovima `Node_1` do `Node_5`
- **Tagiranje** - Sve instance imaju `Project=P2` i `Team=T2` tagove

### Komunikacija

- **TCP socketi** - Length-prefixed JSON poruke preko perzistentnih konekcija
- **Protokol** - 6 tipova poruka: `REQUEST`, `REPLY`, `ELECTION`, `ANSWER`, `COORDINATOR`, `HEARTBEAT`
- **Peer discovery** - Konfiguracijska datoteka `peers.json` (ID → IP:Port mapping)
- **Failure detection** - Timeouts i automatsko označavanje neaktivnih čvorova

### Algoritmi

#### 1. Lamportovi logički satovi

- Svaka poruka nosi timestamp
- Pri slanju: `clock += 1`
- Pri primanju: `clock = max(local_clock, received_clock) + 1`

#### 2. Ricart-Agrawala Mutex

- 3 stanja: `RELEASED`, `WANTED`, `HELD`
- Čvor šalje `REQUEST` svim peerima
- Ulazak u kritičnu sekciju tek kad pristignu svi `REPLY` odgovori
- Deferred replies - čvor odgađa odgovor ako je u `HELD` ili `WANTED` stanju s manjim timestampom

#### 3. Bully Leader Election

- Najviši ID postaje koordinator
- Heartbeat poruke svakih 2s
- Timeout od 5s pokreće izbor
- `ELECTION` poruke šalju se višim ID-ovima
- Ako nema `ANSWER`, čvor postaje koordinator i šalje `COORDINATOR` poruke

---

## Upute za pokretanje

### Preduvjeti

1. **AWS Academy Learner Lab** - Pristup aktivnom Lab okruženju
2. **Terraform** - Instaliran lokalno ([download](https://www.terraform.io/downloads))
3. **SSH ključ** - `labsuser.pem` kopiran u `~/.ssh/` s pravima `chmod 400`
4. **AWS kredencijali** - Preuzeti iz AWS Academy _AWS Details_ panela

### Konfiguracija AWS kredencijala

Iz AWS Academy Learner Lab sučelja:

1. Kliknite **AWS Details** → **Show** (desno od _AWS CLI_)
2. Kopirajte kredencijale u `~/.aws/credentials`:

```ini
[default]
aws_access_key_id = <VAŠ_ACCESS_KEY>
aws_secret_access_key = <VAŠ_SECRET_KEY>
aws_session_token = <VAŠ_SESSION_TOKEN>
```

### 1. Pokretanje infrastrukture (Deploy)

```bash
cd terraform
terraform init
terraform apply -auto-approve
```

**Trajanje:** 2-3 minute za kreiranje instanci + instalaciju paketa + deploy aplikacije.

**Što se događa:**

1. Terraform kreira 5 EC2 instanci s pripadajućom mrežnom infrastrukturom
2. `user_data.sh.tpl` instalira Python 3 i boto3
3. `terraform_data.node_deployment` kopira `node.py`, `cloudwatch_logger.py`, i `peers.json`
4. `deploy.sh` pokreće svaki čvor u tmux sesiji s `USE_CLOUDWATCH=true`

**Output:**

```
node_ips = {
  "Node 1" = "54.123.45.67"
  "Node 2" = "54.123.45.68"
  ...
}
ssh_quickstart = "ssh -i ~/.ssh/labsuser.pem ubuntu@54.123.45.67"
```

### 2. Monitoring (CloudWatch Logs)

#### Pregled logova:

1. AWS Console → **CloudWatch** → **Logs** → **Log groups**
2. Odaberite grupu: `/Distributed_System_Logs`
3. Odaberite stream po čvoru: `Node_1`, `Node_2`, ..., `Node_5`

#### Što tražiti u logovima:

| Tip događaja     | Opis                        | Primjer                                |
| ---------------- | --------------------------- | -------------------------------------- |
| `CLOCK_UPDATE`   | Lamport sat se ažurira      | `"clock": 42, "received_time": 41`     |
| `SEND_MESSAGE`   | Slanje poruke               | `"type": "REQUEST", "target": 3`       |
| `ENTER_CS`       | Ulazak u kritičnu sekciju   | `"state": "HELD", "request_clock": 15` |
| `EXIT_CS`        | Izlazak iz kritične sekcije | `"deferred_replies": [2, 4]`           |
| `LEADER_UPDATE`  | Nova vođa                   | `"coordinator_id": 5`                  |
| `NODE_DOWN`      | Detektirani kvar            | `"target": 3, "reason": "timeout"`     |
| `ELECTION_START` | Početak izbora vođe         | `"reason": "heartbeat_timeout"`        |

#### CloudWatch Insights primjeri:

**Broj zahtjeva za mutex po čvoru:**

```
fields @timestamp, node_id, event_type
| filter event_type = "ENTER_CS"
| stats count() by node_id
```

**Vrijeme u kritičnoj sekciji:**

```
fields @timestamp, node_id
| filter event_type = "ENTER_CS" or event_type = "EXIT_CS"
| sort @timestamp asc
```

### 3. Demo scenariji i testiranje

#### Scenario 1: Paralelni zahtjevi za kritičnu sekciju

Čvorovi automatski generiraju zahtjeve. Provjerite u CloudWatch logovima da **nikad dva čvora istovremeno nisu u kritičnoj sekciji**.

#### Scenario 2: Simulacija kvara vođe

1. Spojite se na instancu trenutnog koordinatora:

   ```bash
   ssh -i ~/.ssh/labsuser.pem ubuntu@<KOORDINATOR_IP>
   ```

2. Pronađite proces i ubijte ga:

   ```bash
   ps aux | grep node.py
   sudo kill <PID>
   ```

3. U CloudWatch logovima ostalih čvorova pratite:
   - Prestanak `HEARTBEAT` poruka
   - `ELECTION_START` događaj nakon timeoutа (5s)
   - `ELECTION` i `ANSWER` poruke
   - `LEADER_UPDATE` s novim `coordinator_id`

#### Scenario 3: Korištenje admin skripte

Za slanje komandi na čvorove bez SSH-a:

```bash
cd scripts
./admin_script.sh 1 elect   # Pokreni izbor na Node 1
./admin_script.sh 2 req     # Zatraži mutex na Node 2
./admin_script.sh 3 status  # Prikaži status Node 3
```

### 4. Mjerenja performansi

#### Automatski benchmark

Za automatsko mjerenje performansi preko 3 konfiguracije (3, 5 i 7 čvorova):

```bash
cd benchmark
python3 benchmark.py          # 5 zahtjeva po konfiguraciji (default)
python3 benchmark.py 10       # 10 zahtjeva po konfiguraciji
```

**Skripta automatski:**

1. Pokreće čvorove za svaku konfiguraciju
2. Triggerira izbor vođe
3. Šalje mutex zahtjeve
4. Analizira logove
5. Generira izvještaje

**Izlazni fajlovi:**

- `benchmark_results.json` - Sirovi podaci u JSON formatu
- `benchmark_report.md` - Markdown izvještaj s tablicama i analizom

**Primjer izlaza:**

```
==========================================================================================
FINAL RESULTS TABLE
==========================================================================================
Config                Nodes   CS Entries   REQ Msgs   REPLY Msgs   Avg Wait   Max Wait
------------------------------------------------------------------------------------------
3-node cluster            3            5         10           10      0.500s      0.600s
5-node cluster            5            5         20           20      1.000s      1.200s
7-node cluster            7            5         30           30      1.400s      1.600s
------------------------------------------------------------------------------------------

Message complexity analysis (Ricart-Agrawala: 2(N-1) messages per CS request):
  3 nodes: Expected 4 msgs/request
  5 nodes: Expected 8 msgs/request
  7 nodes: Expected 12 msgs/request
==========================================================================================
```

#### Ručna analiza (CloudWatch)

Čvorovi logiraju:

- **Broj poruka** - `SEND_MESSAGE` događaji s brojem poslatih poruka
- **Vrijeme čekanja** - Razlika između `REQUEST` i `ENTER_CS` timestampova

**Konfigurirane konfiguracije za testiranje:**

1. Normalan rad (5 čvorova)
2. Kvar vođe (4 aktivna čvora)
3. Visoka konkurencija (učestali mutex zahtjevi)

**Analiza u CloudWatch Insights:**

```
fields @timestamp, node_id, event_type, details.wait_time_ms
| filter event_type = "MUTEX_STATS"
| stats avg(details.wait_time_ms) as avg_wait, max(details.wait_time_ms) as max_wait by node_id
```

---

## Lokalno pokretanje (Development)

Za razvoj i testiranje bez AWS infrastrukture:

### 1. Priprema okruženja

```bash
cd src
# Osigurajte da imate Python 3.10+
python3 --version
```

### 2. Konfiguracija

Datoteka `src/peers.json` već sadrži lokalnu konfiguraciju:

```json
{
  "1": { "ip": "127.0.0.1", "port": 5001 },
  "2": { "ip": "127.0.0.1", "port": 5002 },
  "3": { "ip": "127.0.0.1", "port": 5003 },
  "4": { "ip": "127.0.0.1", "port": 5004 },
  "5": { "ip": "127.0.0.1", "port": 5005 }
}
```

### 3. Pokretanje čvorova

U **5 zasebnih terminala**:

```bash
# Terminal 1
cd src
python3 node.py --id 1 --peers peers.json

# Terminal 2
cd src
python3 node.py --id 2 --peers peers.json

# ... i tako dalje do Node 5
```

**Napomena:** U lokalnom načinu rada `USE_CLOUDWATCH` je automatski `False` i logovi se ispisuju na stdout u JSON formatu.

### 4. Interakcija

U terminalima čvorova možete upisivati komande:

- `req` - Zatraži ulazak u kritičnu sekciju
- `elect` - Pokreni izbor vođe
- `status` - Prikaži trenutno stanje čvora
- `quit` - Zaustavi čvor

---

## Čišćenje infrastrukture (Cleanup)

**VAŽNO:** Nakon završetka testiranja obavezno uklonite sve AWS resurse da ne trošite kredit!

```bash
cd terraform
terraform destroy -auto-approve
```

**Što se briše:**

- Sve EC2 instance (Node-1 do Node-5)
- Security Group (dist-system-sg)
- CloudWatch log streamovi (log grupa ostaje)

**Dodatno ručno brisanje (opcionalno):**

- CloudWatch Log Group `/Distributed_System_Logs` (AWS Console → CloudWatch → Log groups)

---

## Sigurnost i najbolje prakse

### Sigurnost

- [x] **IAM Least Privilege** - Koristi se postojeći `LabInstanceProfile` s minimalnim potrebnim dozvolama
- [x] **Security Groups** - Ograničena komunikacija samo na potrebne portove
- [x] **Tajne** - Nisu pohranjene u kodu; koriste se environment varijable
- [x] **SSH** - Pristup samo s privatnim ključem (`labsuser.pem`)

### Toleriranje kvarova

- [x] **Failure Detection** - TCP timeout + retry mehanizam
- [x] **Dead Node Tracking** - Thread-safe praćenje neaktivnih čvorova
- [x] **Leader Recovery** - Automatski heartbeat i re-election
- [x] **Mutex Resilience** - Smanjeni quorum ako čvor nije dostupan

### Reproducibilnost

- [x] **IaC** - Terraform konfiguracija s verzioniranim stanjem
- [x] **Automatizacija** - Potpuno automatski deploy od nule do pokretanja
- [x] **Dokumentacija** - Jasne upute za setup, test i teardown
- [x] **Git** - Verzioniranje koda i infrastrukture

---

## Dodatne informacije

### Struktura poruka (JSON)

```json
{
  "sender": 3,
  "type": "REQUEST",
  "timestamp": 42,
  "request_clock": 42
}
```

### Lamport Clock pravila

```python
# Slanje poruke
def tick():
    clock += 1
    return clock

# Primanje poruke
def update_clock(received_time):
    clock = max(clock, received_time) + 1
```

### Ricart-Agrawala Mutex algoritam

```
1. Čvor prelazi u WANTED i šalje REQUEST(clock) svim peerima
2. Peer odgovara REPLY odmah AKO:
   - Je u RELEASED stanju, ILI
   - Je u WANTED ali ima veći (clock, node_id) par
3. Čvor ulazi u HELD kad skupi sve REPLY odgovore
4. Pri izlasku šalje REPLY svim odgođenim requestovima
```

### Bully Election algoritam

```
1. Heartbeat prestane → election_timeout (5s) istekne
2. Čvor šalje ELECTION višim ID-ovima
3. AKO dobije ANSWER → čeka COORDINATOR
4. AKO ne dobije ANSWER → proglašava se koordinatorom
5. Novi koordinator šalje COORDINATOR svima
```

---

## Licenca i autori

**Projekt:** P2 - Distribuirana koordinacija  
**Kolegij:** Distribuirani računalni sustavi  
**Akademska godina:** 2025/2026  
**Tim:** T2

Svi članovi tima su ravnopravno doprinijeli projektu prema definiranim ulogama.
