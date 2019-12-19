def update_board(board_str, mov):
    '''
        Updates board_str (strng) based on the set mov. Mov will
        ALWAYS be true, and updates board_str based on it.
    '''
    # confirmed, mov will ALWAYS be true.
    board_list = []  # made to 
    for i in board_str:
        board_list.append(i)
    # so, take the string at mov[0].
    board_list[mov[0]] = '0'
    # everything between mov[0:2] becomes '0', and mov[2] becomes '1'.
    board_list[mov[1]] = '0'
    board_list[mov[2]] = '1'
    board_str_upd = ''.join(board_list)
    return board_str_upd
