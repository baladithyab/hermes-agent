import { randomUuid } from '@hermes/shared'

// Comparison tokens are issued by the authenticated host, never by the guest.
export interface SelectionOwner {
  profile: string | null
  connectionId: string | null
}
export interface SelectionToken { client: string; scope?: string; epoch: number }
interface Acknowledgment { scope_id: string; epoch: number }
export interface SelectionLease {
  owner: SelectionOwner
  token: SelectionToken
  generation: number
}
type Exchange = (owner: SelectionOwner, action: 'selection' | 'deselection', token: SelectionToken) => Promise<Acknowledgment>

export class PluginSelectionSession {
  private generation = 0
  private desired: string | null = null
  private selected: SelectionLease | null = null
  private tail: Promise<void> = Promise.resolve()
  private routes = new Map<string, SelectionToken>()
  private exchange: Exchange
  private clientId: () => string

  constructor(exchange: Exchange, clientId: () => string = randomUuid) {
    this.exchange = exchange
    this.clientId = clientId
  }

  change(owner: SelectionOwner): void {
    const captured = { ...owner }
    const key = JSON.stringify(captured)

    if (key === this.desired) {return}
    this.desired = key
    const generation = ++this.generation
    // Do not recover an ambiguous write by silently minting a new client.
    // A lost ACK requires explicit connection recovery, not a blind retry.
    this.tail = this.tail.then(async () => {
      if (this.selected) {
        const old = this.selected
        const ack = await this.exchange(old.owner, 'deselection', old.token)
        this.routes.set(JSON.stringify(old.owner), this.validate(old.token, ack))
        this.selected = null
      }

      const token = this.routes.get(key) ?? { client: this.clientId(), epoch: 0 }
      const ack = await this.exchange(captured, 'selection', token)
      const acknowledged = this.validate(token, ack)
      this.routes.set(key, acknowledged)
      this.selected = { owner: captured, token: acknowledged, generation }
    })
    // Observe background switch failures without turning the rejected chain green.
    void this.tail.catch(() => undefined)
  }

  private validate(token: SelectionToken, ack: Acknowledgment): SelectionToken {
    if (!ack || !/^[a-f0-9]{32}$/.test(ack.scope_id) || !Number.isSafeInteger(ack.epoch) || ack.epoch <= token.epoch) {
      throw new Error('STALE_SCOPE')
    }

    return { client: token.client, scope: ack.scope_id, epoch: ack.epoch }
  }

  async acquire(): Promise<SelectionLease> {
    const generation = this.generation
    await this.tail

    if (!this.selected || generation !== this.generation || this.selected.generation !== generation) {
      throw new Error('STALE_SCOPE')
    }

    return this.selected
  }

  current(lease: SelectionLease): boolean {
    return lease.generation === this.generation && lease === this.selected
  }
}
