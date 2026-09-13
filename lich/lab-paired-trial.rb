# frozen_string_literal: true

# Opt-in adapter for a reviewed native GroupTrial runner. This module neither
# launches scripts nor sends commands. The runner retains all game policy and
# must call poll/finish on its owner thread outside disposable work guards.
module LabPairedTrial
  class Invalid < StandardError; end

  class Runtime
    CAPACITY = 32

    # native is EO::Engine::Controller; its Guard and Immutable implementations
    # are reused, not reimplemented. launch has already passed Launch.extract!.
    def initialize(native:, owner:, launch:, identity:, snapshot:, clock:)
      @native, @owner, @launch, @identity, @snapshot, @clock = native, owner, launch,
        native::Immutable.copy(identity), snapshot, clock
      @thread, @mutex, @mailbox = Thread.current, Mutex.new, []
      @state, @phase, @reason = :starting, 'outbound', nil
      @closed, @activated, @return_requested = false, false, false
      raise Invalid, 'paired controller identity must be an exact session pin' unless valid_identity?(@identity)
      @origin = native::Immutable.copy(snapshot.call)
      raise Invalid, 'paired controller requires safe original refuge state' unless safe_snapshot?(@origin)
      @guard = native::Guard.new(owner: owner, snapshot: snapshot, launch: launch, clock: clock)
      @guard.bind_session!(@identity.fetch(:session))
      publish
    end

    # Same exact-runtime activation seam used by LAB's existing Binding/Lease.
    def activate_supervised(valid:)
      @mutex.synchronize do
        return false if @closed || @activation_attempted
        @activation_attempted = true
      end
      admitted = @guard.activate(valid)
      # Work sends keep their separate, disposable native guards. This guard
      # supplies the absolute local authority ceiling, including authorized return.
      @guard.phase = :return if admitted
      @mutex.synchronize { @activated = admitted }
      admitted
    end

    def status = @mutex.synchronize { @cached }

    # Callback threads only enqueue intent. They never inspect game state or
    # call GroupTrial. Hold/resume are deliberately not offered in this pilot.
    def request(action, valid: nil)
      @mutex.synchronize do
        return response(false, 'invalid_predicate') unless valid.nil? || valid.is_a?(Proc)
        return response(false, 'unknown_control') unless %w[status stop retreat].include?(action.to_s)
        return response(true) if action.to_s == 'status'
        return response(false, 'run_closed') if @closed
        return response(false, 'control_queue_full') if @mailbox.size >= CAPACITY
        @mailbox << [action.to_s.freeze, valid].freeze
        response(true)
      end
    end

    def await_supervisor!
      owner_thread!
      deadline = [@clock.call + 3, @launch.work_deadline].min
      until @mutex.synchronize { @activated }
        raise Invalid, 'paired startup activation expired' if @clock.call >= deadline
        raise Invalid, 'paired startup state changed' unless safe_snapshot?(@snapshot.call)
        @owner.execution_sleep(0.01)
      end
      raise Invalid, 'paired startup authority unavailable' unless permitted?
      @state = :running
      publish
      true
    end

    def permitted?
      !@mutex.synchronize { @closed } && @guard.permitted?(nil)
    end

    # A queued control interrupts an in-flight action at its next checkpoint.
    # poll rechecks the predicate before turning that interruption into a return.
    def work_permitted?
      permitted? && @clock.call < @launch.work_deadline &&
        @mutex.synchronize { @mailbox.empty? && !@return_requested }
    end

    # Return intent, not execution: caller applies this via Participant.request_stop.
    # No work budget or authority is renewed by polling.
    def poll(local: nil)
      owner_thread!
      raise Invalid, 'paired controller authority lost' unless permitted?
      pending = @mutex.synchronize { @mailbox.shift(CAPACITY) }
      requested = pending.any? do |_action, predicate|
        predicate.nil? || predicate.call == true
      rescue StandardError
        false
      end
      reason = requested ? 'ordinary_stop' : nil
      reason ||= 'operation_work_deadline' if @clock.call >= @launch.work_deadline
      if local
        @phase = { waiting: 'outbound', working: 'working', returning: 'returning', finished: 'returning' }.fetch(local[:phase])
        @reason = local[:reason]
      end
      @mutex.synchronize { @return_requested = true } if reason
      publish
      reason
    end

    # The reviewed rendezvous supplies two exact pins, not names rediscovered
    # later. No transport token or remote executable description enters status.
    def bind_pair!(identities)
      owner_thread!
      raise Invalid, 'pair already bound' if @pair
      unless identities.is_a?(Array) && identities.size == 2 && identities.include?(@identity) &&
             identities.all? { |pin| valid_identity?(pin) } &&
             identities.map { |pin| pin[:character].downcase }.uniq.size == 2
        raise Invalid, 'pair identities do not include this exact controller'
      end
      @pair = @native::Immutable.copy(identities)
    end

    # Call only after exact native child cleanup. Both immutable native receipts
    # and a fresh local observation are required. Safe recovery is independent
    # from successful combat; an absent peer never becomes a successful test.
    def finish(local:, shared:, cleaned:)
      owner_thread!
      return status if @mutex.synchronize { @closed }
      # Missing final evidence must not abort the runner's remaining teardown.
      observed = begin
        @snapshot.call
      rescue StandardError
        nil
      end
      safe = cleaned == true && permitted? && safe_snapshot?(observed) &&
        observed[:owner_released] == true && observed[:children_released] == true &&
        receipt?(local, @identity) && local[:hands] == @origin[:hands]
      receipts = shared.is_a?(Hash) ? shared[:receipts] : nil
      pair_safe = @pair && receipts.is_a?(Array) && receipts.size == 2 &&
        @pair.all? { |pin| receipts.one? { |receipt| receipt?(receipt, pin) } } && receipts.include?(local)
      success = safe && pair_safe && shared[:complete] == true && shared[:success] == true &&
        shared[:reason] == 'kill_limit' && shared[:target_id].to_s.match?(/\A[1-9]\d*\z/)
      @state, @phase = success ? :completed : :stopped, 'finished'
      @reason = success ? 'completed' : safe ? 'trial_failed' : 'unsafe_handoff'
      @work_result = { state: success ? :completed : :stopped, reason: shared.is_a?(Hash) ? shared[:reason] : 'missing_pair_result' }
      @returned = @restored = safe == true
      @pair_complete, @pair_safe = shared.is_a?(Hash) && shared[:complete] == true, pair_safe == true
      @mutex.synchronize { @closed = true }
      @guard.revoke!
      publish
    end

    # No inferred recovery after exception/forced exit. Never invoke a command.
    def close
      owner_thread!
      return status if @mutex.synchronize { @closed }
      @state, @phase, @reason = :stopped, 'finished', 'adapter_closed'
      @guard.revoke!
      @mutex.synchronize { @closed = true }
      publish
    end

    private

    def owner_thread!
      raise ThreadError, 'paired adapter must run on its owner thread' unless Thread.current.equal?(@thread)
    end

    def valid_identity?(pin)
      pin.is_a?(Hash) && pin.keys.sort == %i[character run_id session] &&
        pin.values.all? { |v| v.is_a?(String) && !v.empty? && v.bytesize <= 128 }
    end

    def safe_snapshot?(value)
      value.is_a?(Hash) && %i[connected alive owner stable ready standing].all? { |key| value[key] == true } &&
        value[:room_id] == @launch.refuge_room && value[:session] == @identity[:session] &&
        value[:hands].is_a?(Array) && value[:hands].size == 2 &&
        value[:hands].all? { |id| id.nil? || (id.is_a?(String) && id.match?(/\A[1-9]\d*\z/)) } &&
        (!@origin || value[:hands] == @origin[:hands])
    end

    def receipt?(receipt, identity)
      receipt.is_a?(Hash) && receipt[:identity] == identity && receipt[:safe] == true &&
        receipt[:refuge_room] == @launch.refuge_room
    end

    def response(accepted, reason = nil)
      { accepted: accepted, reason: reason, status: @cached }.freeze
    end

    def publish
      value = @native::Immutable.copy(
        mode: 'paired_trial', state: @state, reason: @reason,
        retreat_pending: @phase == 'returning', work_result: @work_result,
        pair: { complete: @pair_complete == true, safe: @pair_safe == true },
        refuge: { room_id: @launch.refuge_room, phase: @phase,
                  returned: @returned == true, equipment_restored: @restored == true },
        timing: { updated_at: @clock.call, work_deadline: @launch.work_deadline,
                  cleanup_deadline: @launch.cleanup_deadline, return_deadline: @launch.return_deadline }
      )
      @mutex.synchronize { @cached = value }
      value
    end
  end
end
