# Arhitekturni dijagram - P2 Distribuirana koordinacija

## Pregled sustava

Ovaj dijagram prikazuje arhitekturu distribuiranog sustava s 5 čvorova koji implementiraju Lamportove logičke satove, Ricart-Agrawala međusobno isključivanje i Bully algoritam za izbor vođe.

## Dijagram infrastrukture

```mermaid
flowchart TB
    subgraph AWS["AWS Cloud (us-east-1)"]
        subgraph VPC["Default VPC"]
            subgraph SG["Security Group (dist-system-sg)"]
                subgraph Nodes["EC2 Instance (t3.micro, Ubuntu 24.04)"]
                    N1["Node 1<br/>TCP :5000"]
                    N2["Node 2<br/>TCP :5000"]
                    N3["Node 3<br/>TCP :5000"]
                    N4["Node 4<br/>TCP :5000"]
                    N5["Node 5<br/>TCP :5000"]
                end
            end
        end

        subgraph CloudWatch["CloudWatch Logs"]
            LG["/Distributed_System_Logs"]
            S1["Stream: Node_1"]
            S2["Stream: Node_2"]
            S3["Stream: Node_3"]
            S4["Stream: Node_4"]
            S5["Stream: Node_5"]
            LG --> S1 & S2 & S3 & S4 & S5
        end

        subgraph Monitoring["CloudWatch Alarmi"]
            MF["Metric Filter<br/>(Error Events)"]
            AL["⚠️ Alarm<br/>distributed-system-error-alarm"]
            MF --> AL
        end

        IAM["IAM: LabInstanceProfile"]
    end

    %% TCP mesh connections
    N1 <-->|"TCP"| N2
    N1 <-->|"TCP"| N3
    N1 <-->|"TCP"| N4
    N1 <-->|"TCP"| N5
    N2 <-->|"TCP"| N3
    N2 <-->|"TCP"| N4
    N2 <-->|"TCP"| N5
    N3 <-->|"TCP"| N4
    N3 <-->|"TCP"| N5
    N4 <-->|"TCP"| N5

    %% CloudWatch connections
    N1 -.->|"boto3"| S1
    N2 -.->|"boto3"| S2
    N3 -.->|"boto3"| S3
    N4 -.->|"boto3"| S4
    N5 -.->|"boto3"| S5

    LG --> MF

    %% IAM connection
    IAM -.->|"permissions"| Nodes

    %% External
    Admin["Administrator<br/>(SSH + admin_script.sh)"]
    Admin -->|"SSH :22"| N1 & N2 & N3 & N4 & N5

    TF["Terraform<br/>(IaC)"]
    TF -->|"provision"| AWS
```

## Dijagram komunikacijskog protokola

```mermaid
flowchart LR
    subgraph Protocol["Message Types (JSON over TCP)"]
        direction TB
        REQ["REQUEST<br/>Mutex zahtjev"]
        REP["REPLY<br/>Mutex odobrenje"]
        ELE["ELECTION<br/>Početak izbora"]
        ANS["ANSWER<br/>Odgovor višeg čvora"]
        COO["COORDINATOR<br/>Objava vođe"]
        HB["HEARTBEAT<br/>Liveness provjera"]
    end
```

## Dijagram algoritama

```mermaid
stateDiagram-v2
    [*] --> RELEASED: Inicijalno stanje

    state "Ricart-Agrawala Mutex" as Mutex {
        RELEASED --> WANTED: request_critical_section()
        WANTED --> HELD: Primljeni svi REPLY
        HELD --> RELEASED: exit_critical_section()
    }

    state "Bully Election" as Election {
        state "Čekanje" as Wait
        state "Izbor u tijeku" as InProgress
        state "Koordinator" as Coordinator

        Wait --> InProgress: Timeout / start_election()
        InProgress --> Coordinator: Nema višeg čvora
        InProgress --> Wait: Primljen ANSWER
        Coordinator --> Wait: Kvar
    }
```

## Dijagram komponenti čvora

```mermaid
flowchart TB
    subgraph Node["DistributedNode (node.py)"]
        subgraph Core["Jezgra"]
            LC["Lamport Clock<br/>(tick, update_clock)"]
            SM["Send/Receive<br/>(TCP sockets)"]
        end

        subgraph Mutex["Ricart-Agrawala"]
            MS["State: RELEASED|WANTED|HELD"]
            DR["Deferred Replies"]
            RR["Replies Received"]
        end

        subgraph Leader["Bully Election"]
            ES["Election State"]
            HB["Heartbeat Loop"]
            TO["Timeout Detection"]
        end

        subgraph Logging["Observability"]
            CW["CloudWatch Logger"]
            JS["JSON Structured Logs"]
        end

        LC --> SM
        SM --> Mutex
        SM --> Leader
        Mutex --> CW
        Leader --> CW
        CW --> JS
    end

    Peers["Peer Nodes"] <-->|"TCP :5000"| SM
    JS -->|"boto3"| CloudWatch["CloudWatch"]
```

## Dijagram sekvence - Mutex zahtjev

```mermaid
sequenceDiagram
    participant N1 as Node 1
    participant N2 as Node 2
    participant N3 as Node 3
    participant CS as Critical Section

    Note over N1,N3: Lamport satovi se ažuriraju pri svakoj poruci

    N1->>N1: state = WANTED, clock++
    N1->>N2: REQUEST (timestamp=5)
    N1->>N3: REQUEST (timestamp=5)

    N2->>N2: clock = max(clock, 5) + 1
    N2->>N1: REPLY

    N3->>N3: clock = max(clock, 5) + 1
    N3->>N1: REPLY

    N1->>N1: Primljeni svi REPLY
    N1->>CS: ENTER Critical Section
    Note over N1,CS: state = HELD
    N1->>CS: EXIT Critical Section
    N1->>N1: state = RELEASED
```

## Dijagram sekvence - Bully Election

```mermaid
sequenceDiagram
    participant N2 as Node 2
    participant N3 as Node 3
    participant N4 as Node 4
    participant N5 as Node 5 (Leader)

    Note over N2,N5: Node 5 je trenutni koordinator

    N5->>N5: Kvar / Shutdown

    Note over N2,N4: Timeout - nema heartbeat

    N3->>N4: ELECTION
    N3->>N5: ELECTION (no response)

    N4->>N3: ANSWER
    N4->>N5: ELECTION (no response)

    Note over N4: Čeka ANSWER, timeout

    N4->>N4: Postaje koordinator
    N4->>N2: COORDINATOR
    N4->>N3: COORDINATOR

    Note over N2,N4: Node 4 je novi vođa

    loop Heartbeat (2s interval)
        N4->>N2: HEARTBEAT
        N4->>N3: HEARTBEAT
    end
```

## Tagiranje resursa

| Resurs                          | Tag: Project | Tag: Team |
| ------------------------------- | ------------ | --------- |
| EC2 Instance (Node-1 do Node-5) | P2           | T2        |
| CloudWatch Log Group            | P2           | T2        |
| Security Group                  | -            | -         |

## Sigurnosna pravila (Security Group)

| Smjer   | Port | Protokol | Izvor/Odredište   |
| ------- | ---- | -------- | ----------------- |
| Ingress | 22   | TCP      | 0.0.0.0/0 (SSH)   |
| Ingress | 5000 | TCP      | self (inter-node) |
| Egress  | All  | All      | 0.0.0.0/0         |
