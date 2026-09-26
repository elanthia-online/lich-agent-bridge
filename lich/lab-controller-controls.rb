# frozen_string_literal: true

# Exact-run local control admission. The authority reader runs only in bridge
# workers; the owner's queued predicate reads cached state and local pins only.
module LabControllerControls
  class Invalid < StandardError; end

  class Lease
    MAX_CACHE_AGE = 0.25

    def initialize(action:, available:, authority:, clock:, deadline: nil)
      @action = action.transform_values { |value| value.is_a?(String) ? value.dup.freeze : value }.freeze
      @available, @authority, @clock = available, authority, clock
      @deadline = deadline || @action.fetch(:expires_at)
      @mutex, @checked_at, @revoked = Mutex.new, nil, false
      @return_requested = false
    end

    def refresh
      observed = @authority.call(@action.fetch(:action_id))
      valid = observed.is_a?(Hash) &&
              %i[action_id character generation command expected_room_id expires_at controller_deadline].all? { |key| observed[key] == @action[key] } &&
              %w[dispatched completed].include?(observed[:status]) && observed[:stop_requested] == false
      @mutex.synchronize do
        @revoked = true unless valid
        @checked_at = @clock.call if valid && !@revoked
        @return_requested = true if valid && observed[:return_requested] == true
      end
      valid?
    rescue StandardError
      @mutex.synchronize { @revoked = true }
      false
    end

    def valid?
      cached = @mutex.synchronize do
        !@revoked && @checked_at && (@clock.call - @checked_at).between?(0, MAX_CACHE_AGE) &&
          @clock.call < @deadline
      end
      cached == true && @available.call(@action) == true
    rescue StandardError
      false
    end

    def expired?
      @clock.call >= @deadline
    end

    def return_requested?
      @mutex.synchronize { @return_requested }
    end

    def revoke
      @mutex.synchronize { @revoked = true }
    end
  end

  class Binding
    CAPACITY = 32
    attr_reader :instance, :run_id, :controller

    def initialize(controller:, launch:, instance:, available:, authority:, clock: -> { Time.now.to_f })
      @controller, @instance = controller, instance
      @launch = launch.transform_values { |value| value.is_a?(String) ? value.dup.freeze : value }.freeze
      @run_id = @launch.fetch(:action_id)
      @available, @authority, @clock = available, authority, clock
      @mutex, @leases, @runtime, @closed = Mutex.new, [], nil, false
    end

    def bind(instance, runtime)
      raise Invalid, 'control runtime does not belong to the exact launched instance' unless @instance.equal?(instance)
      raise Invalid, 'control runtime requires request and status' unless runtime.respond_to?(:request) && runtime.respond_to?(:status)
      @mutex.synchronize do
        raise Invalid, 'control runtime is already bound or closed' if @closed || @runtime
        @runtime = runtime
      end
      true
    end

    def close
      @mutex.synchronize { @closed = true }
    end

    def open?
      @mutex.synchronize { !@closed }
    end

    def refresh
      leases = @mutex.synchronize do
        @leases.reject!(&:expired?)
        @leases.dup
      end
      leases.each(&:refresh)
    end

    def request(action:, match:)
      raise Invalid, 'control belongs to another run' unless match.controller.name == @controller.name && match.arguments['run_id'] == @run_id
      raise Invalid, 'control identity changed' unless %i[character generation].all? { |key| action[key] == @launch[key] }
      raise Invalid, 'control is not registered' unless match.action.kind == 'control'
      canonical = match.action.build(match.arguments).fetch(:command)
      raise Invalid, 'control command does not match registration' unless action[:command] == canonical
      runtime = @mutex.synchronize do
        @leases.reject!(&:expired?)
        raise Invalid, 'control runtime is unavailable' if @closed || !@runtime
        raise Invalid, 'control queue is full' if @leases.length >= CAPACITY
        @runtime
      end
      lease = Lease.new(action: action, authority: @authority, clock: @clock,
                        available: ->(request) { open? && @available.call(request) == true })
      raise Invalid, 'control authority unavailable or revoked' unless lease.refresh
      @mutex.synchronize do
        raise Invalid, 'control runtime is unavailable' if @closed
        raise Invalid, 'control queue is full' if @leases.length >= CAPACITY
        @leases << lease
      end
      response = runtime.request(match.action.name, valid: -> { lease.valid? })
      raise Invalid, 'control runtime rejected request' unless response.is_a?(Hash) && response[:accepted] == true
      { ok: true, code: match.action.name == 'status' ? 'control_status' : 'control_queued',
        message: 'Control admitted; application and safe handoff are not established.',
        details: { run_id: @run_id, control_action_id: action.fetch(:action_id),
                   control: match.action.name, applied: nil, status: response[:status] } }
    rescue StandardError
      lease&.revoke
      @mutex.synchronize { @leases.delete(lease) } if lease
      raise
    end
  end
end
