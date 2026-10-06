"""TC-06 / TC-07: the framework security gates must not be bypassable.

Every node in this template subclasses FunctionNode, which applies an input gate
before execute() and an output gate after it. A node extends those gates through
the documented _extra_security_gate_input() / _extra_security_gate_output()
hooks; replacing the gates themselves is refused at class-definition time, which
is what these two cases pin. Without that guarantee, every other boundary test
in this suite would be asserting behaviour a subclass could opt out of.
"""

import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class TestFunctionNodeFinalSecurityGates:
    """TC-06/TC-07: the FunctionNode security gates are non-bypassable."""

    def test_tc06_security_gate_input_cannot_be_overridden(self):
        """Overriding the default input gate raises at class definition."""
        with pytest.raises(TypeError, match="_security_gate_input"):

            class _InvalidInputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_input(self, state):
                    return state

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}

    def test_tc07_security_gate_output_cannot_be_overridden(self):
        """Overriding the default output gate raises at class definition."""
        with pytest.raises(TypeError, match="_security_gate_output"):

            class _InvalidOutputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_output(self, result):
                    return result

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}
