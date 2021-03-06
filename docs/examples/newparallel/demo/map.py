from IPython.parallel import *

client = Client()
view = client[:]

@view.remote(block=True)
def square(a):
    """return square of a number"""
    return a*a

squares = list(map(square, list(range(42))))

# but that blocked between each result; not exactly useful

square.block = False

arlist = list(map(square, list(range(42))))
# submitted very fast

# wait for the results:
squares2 = [ r.get() for r in arlist ]

# now the more convenient @parallel decorator, which has a map method:

@view.parallel(block=False)
def psquare(a):
    """return square of a number"""
    return a*a

# this chunks the data into n-negines jobs, not 42 jobs:
ar = psquare.map(list(range(42)))

# wait for the results to be done:
squares3 = ar.get()

print(squares == squares2, squares3==squares)
# True