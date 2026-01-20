# P2 - Distribuirana koordinacija (Tim T2)

Ovaj repozitorij sadrži implementaciju distribuiranog sustava s 5 čvorova koji demonstriraju:

1. Lamportove satove (logičko vrijeme).
2. Ricart-Agrawala algoritam (međusobno isključivanje).
3. Bully algoritam (izbor vođe).

## Struktura tima

- Vedran Marić: Voditelj, integracija.
- Anđela Marinović: Infrastruktura (AWS).
- Leo Petrović: Algoritmi (Mutex/Lamport).
- Nikola Pehar: Leader Election, mjerenja.

## Preduvjeti

- AWS Academy Learner Lab pristup.
- Terraform instaliran.
- Python 3 + boto3.

## Upute za pokretanje

### 1. Podizanje infrastrukture (Anđela)

1. Pozicionirajte se u folder s main.tf.
2. Pokrenite:  
   terraform init  
   terraform apply
3. Zapišite javne i privatne IP adrese koje Terraform ispiše (ili pogledajte u AWS konzoli).

### 2. Konfiguracija mreže (Svi)

Kreirajte datoteku peers.json. VAŽNO: Koristite privatne IP adrese ako pokrećete na AWS-u unutar istog VPC-a.

Primjer peers.json:

{  
    "1": {"ip": "172.31.x.1", "port": 5000},  
    "2": {"ip": "172.31.x.2", "port": 5000},  
    "3": {"ip": "172.31.x.3", "port": 5000},  
    "4": {"ip": "172.31.x.4", "port": 5000},  
    "5": {"ip": "172.31.x.5", "port": 5000}  
}

### 3. Pokretanje čvorova

Spojite se SSH-om na svaku instancu, prebacite node.py i peers.json te pokrenite:

Na instanci 1:

python3 node.py --id 1 --peers peers.json

Na instanci 2:

python3 node.py --id 2 --peers peers.json

... i tako dalje za svih 5.

Također možete pokrenuti `python run_local.py` komandu unutar `src/` direktorija da pokrenete svih N čvorova (onoliko koliko ih je definirano u `peers.json`) umjesto da svaki pokrećete jedan po jedan. U tom slučaju, da biste poslali komandu nekom čvoru, morate dodati i ID čvora (npr. umjesto `req`, pišite `req 1` da pošaljete čvoru 1).

### 4. Monitoring

Svaki čvor spašava strukturirane logove u direktoriju `logs/`.

Koristite `python log_viewer.py --files ./logs/* --sort time --follow --interval 0.2` komandu unutar `src/` direktorija kako biste vidjeli zajednički formatirani ispis svih čvorova.

## Čišćenje (Cleanup)

Nakon dema obavezno pokrenuti:

terraform destroy
