import functools
from typing import Dict, List, Optional

from eth_typing import ChecksumAddress
from nucypher_core import RetrievalKit, TreasureMap
from nucypher_core.umbral import PublicKey

from porter import schema


def attach_schema(schema):
    def callable(func):
        func._schema = schema()

        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapped

    return callable


class ControlInterface:

    def __init__(self, implementer=None, *args, **kwargs):
        self.implementer = implementer
        super().__init__(*args, **kwargs)


class PorterInterface(ControlInterface):
    def __init__(self, porter: 'Porter' = None, *args, **kwargs):
        super().__init__(implementer=porter, *args, **kwargs)

    #
    # Alice Endpoints
    #
    @attach_schema(schema.AliceGetUrsulas)
    def get_ursulas(self,
                    quantity: int,
                    exclude_ursulas: Optional[List[ChecksumAddress]] = None,
                    include_ursulas: Optional[List[ChecksumAddress]] = None) -> Dict:
        ursulas_info = self.implementer.get_ursulas(
            quantity=quantity,
            exclude_ursulas=exclude_ursulas,
            include_ursulas=include_ursulas,
        )

        return {"ursulas": ursulas_info}

    @attach_schema(schema.AliceRevoke)
    def revoke(self) -> dict:
        # Steps (analogous to nucypher.character.control.interfaces):
        # 1. creation of objects / setup
        # 2. call self.implementer.some_function() i.e. Porter learner has an associated function to call
        # 3. create response
        pass

    @attach_schema(schema.BobRetrieveCFrags)
    def retrieve_cfrags(self,
                        treasure_map: TreasureMap,
                        retrieval_kits: List[RetrievalKit],
                        alice_verifying_key: PublicKey,
                        bob_encrypting_key: PublicKey,
                        bob_verifying_key: PublicKey,
                        context: Optional[Dict] = None) -> Dict:
        retrieval_outcomes = self.implementer.retrieve_cfrags(
            treasure_map=treasure_map,
            retrieval_kits=retrieval_kits,
            alice_verifying_key=alice_verifying_key,
            bob_encrypting_key=bob_encrypting_key,
            bob_verifying_key=bob_verifying_key,
            context=context,
        )
        return {"retrieval_results": retrieval_outcomes}
